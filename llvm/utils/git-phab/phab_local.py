# ======- phab_local.py - Local client utilities -------*- python -*--========#
#
# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# ==------------------------------------------------------------------------==#

"""Helpers for local Git client (with pending changes).

This file contains helpers for determining what to send to Phabricator.

The Conduit API (see: phab_conduit.py) has some specific corner cases of what
needs to be sent to get the best result in the web UI. Some of the helpers below
may seem quite specific, but the goal is to get to exactly what is needed.
"""

import collections
import json
import os
import subprocess
import sys
import tempfile


################################################################################
# Exception types
################################################################################

class Error(Exception):
    """Exception inside local_workspace."""


class ReferenceError(Error):
    """Invalid reference."""


################################################################################
# Config files
################################################################################


def get_arcrc():
    """Loads (from JSON) and returns the user's .arcrc file."""
    if sys.platform == 'win32':
        arcrc_path = os.path.join(os.getenv('APPDATA'), '.arcrc')
    else:
        arcrc_path = os.path.join(os.getenv('HOME'), '.arcrc')

    with open(arcrc_path) as fh:
        return json.load(fh)

def get_arcconfig():
    """Loads (from JSON) and returns the working copy's .arcconfig file."""
    git_dir = git('rev-parse', '--show-toplevel')
    with open(os.path.join(git_dir, '.arcconfig')) as fh:
        return json.load(fh)


################################################################################
# Git helpers
################################################################################

def git(*argv, decode=True, trim=True):
    """Runs git.

    Args:
        argv: the args to pass (after 'git').
        decode: if True (the default), decode the output to UTF-8.
        trim: if True (the default), trim the output with str.strip().

    Returns:
        The output from git.
    """
    output = subprocess.check_output(('git',) + argv)
    if decode:
        output = output.decode('utf-8')
    if trim:
        output = output.strip()
    return output


def git_tab(*argv, fieldsep=' ', recsep=os.linesep, trim=True,
            skip_empty=True):
    """Splits output and returns as a list-of-records.

    The 'fieldsep' and 'recsep' arguments should match the format that git will
    use. For example:

        git_tabulate('log', '--format=%H%x01%T%00', '-m', '2',
                     fieldsep='\x01', recsep='\x00')
        ==> [['123a...', '234b...'],
             ['345c...', '567d...']]

    Args:
        argv: args to git (after 'git').
        fieldsep: the separator between fields.
        recsep: the separator between records.
        trim: if True (the default), trim a leading newline from records. This
            is useful for cases where the formatted output contains a newline
            in addition to the separator in the format string.
        skip_empty: if True (the default), empty records will be skipped.

    Returns:
        A list of lists.
    """
    # We might not want to decode output immedately if the separators are not
    # valid UTF-8. However, make sure to decode later so that the strings are
    # how we otherwise expect.
    if type(recsep) is not bytes:
        recsep = recsep.encode()
    if type(fieldsep) is not bytes:
        fieldsep = fieldsep.encode()

    if trim:
        linesep = os.linesep.encode()

    output = git(*argv, decode=False)
    records = []
    for line in output.split(recsep):
        if skip_empty and not line.strip():
            continue
        if trim and line.startswith(linesep):
            line = line[len(linesep):]
        records.append([x.decode() for x in line.split(fieldsep)])
    return records


def get_dirty_status():
    """Returns any dirty files in the workspace."""
    return git('status', '--porcelain', trim=False)


def get_symbolic_ref(ref):
    """Returns the symbolic name for the given ref."""
    try:
        return git('rev-parse', '--abbrev-ref', '--symbolic-full-name',
                   ref).strip()
    except subprocess.CalledProcessError as e:
        raise ReferenceError(ref, e.output)


def get_ref_hash(ref):
    """Returns the hash of the given named ref."""
    try:
        return git('rev-parse', ref).strip()
    except subprocess.CalledProcessError as e:
        raise ReferenceError(ref, e.stdout)


def get_upstream_base():
    """Returns the upstream base for this working copy."""
    try:
        upstream_ref = get_symbolic_ref('@{upstream}')
    except ReferenceError:
        # Prompt below.
        pass
    else:
        return upstream_ref

    print('No upstream is configured for current branch.')
    print()
    tracked = git_tab(
        'for-each-ref',
        ('--format=%(if)%(upstream)'
         '         %(then)%(refname:short) %(upstream:short)%(end)'),
        'refs/heads')
    if tracked:
        print('These branches have an upstream set:')
        max_len = max(len(x) for x, _ in tracked)
        for branch, upstream in tracked:
            print('  {branch:{max_len}} [{upstream}]'.format(
                branch=branch, upstream=upstream, max_len=max_len))
        print()

    print('Consider running `git branch -u origin/master` (or similar) '
          'to set a default for this branch.')
    print()
    upstream_ref = input('Upstream branch [origin/master]: ')
    if not upstream_ref:
        upstream_ref = 'origin/master'

    return upstream_ref


def get_diff_base(base_ref, head_commit):
    """Returns the commit to diff against.

    Args:
        base_ref: if None, use @{upstream}.
        head_commit: the HEAD commit.

    Returns:
        The base commit hash.
    """
    if not base_ref:
        base_ref = get_upstream_base()
    return git('merge-base', base_ref, head_commit)


def get_raw_diff(base, commit):
    """Returns the text of a raw diff from base..commit."""
    return git(
        'diff',
        '--no-ext-diff',
        '--color=never',
        '--src-prefix=a/',
        '--dst-prefix=b/',
        '-U32767',  # Include the whole file.
        '--find-renames',
        '--find-copies',
        base,
        commit)


def run_editor(filename=None, fh=None, content=None):
    """Runs $EDITOR to modify some text.

    Exactly one of the args should be provided.

    Args:
        filename: if given, a string: edit this file and return None.
        fh: if given, a file-like object: edit this file and return None.
        content: if given, a string: edit this and return its edited value.
    """
    assert sum(x is not None for x in [content, filename, fh]) == 1, (
        "exactly one argument should be provided")
    editor = os.getenv('EDITOR')

    if fh is not None:
        filename = fh.name

    if filename is not None:
        subprocess.check_call([editor, filename])
        return

    with tempfile.NamedTemporaryFile(prefix='phab_commit') as fh:
        fh.write(content.encode())
        fh.flush()
        fh.seek(0)
        subprocess.check_call([editor, fh.name])
        return fh.read().decode()


def update_head_commit_message(new_message=None, message_file=None):
    """Updates the HEAD commit message.

    Args:
        new_message: the commit message to use.
        message_file: use the message in this file-like object instead.

    Returns:
        The new commit info for HEAD.
    """
    temp_fh = None

    try:
        if message_file is not None:
            assert new_message is None, "must provide exactly one arg"
            filename = message_file.name

        else:
            assert new_message is not None, "must provide a new message"
            temp_fh = tempfile.NamedTemporaryFile(prefix='phab_commit')
            temp_fh.write(new_message.encode())
            temp_fh.flush()
            filename = temp_fh.name

        git('commit', '--amend', '-F', filename)

    finally:
        if temp_fh is not None:
            temp_fh.close()

    return git_log('-m', '1', 'HEAD')


# LocalCommit is information about a commit taken from the local workspace.
#
# The various fields can be read from `git log`. The formats are below.
CommitInfo = collections.namedtuple(
    'CommitInfo',
    ['commit',       # %H
     'tree',         # %T
     'parents',      # %P
     'time',         # %at
     'author',       # %an
     'authorEmail',  # %aE
     'summary',      # %s
     'message',      # %B
    ])


def git_log(*refs):
    """Returns `CommitInfo`s for 'git log' output."""
    commit_info_format = '%x01'.join(['%H', '%T', '%P', '%at', '%an', '%aE',
                                      '%s', '%B']) + '%x00'
    return [
        CommitInfo._make(x)
        for x in git_tab('log', ('--format='+commit_info_format), *refs,
                         fieldsep=b'\x01', recsep=b'\x00')
    ]


def get_local_commits(base):
    """Returns information about local commits (not on the upstream ref)."""
    head = get_ref_hash('HEAD')
    return git_log(head, '--not', base)
