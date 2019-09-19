# ======- phab_conduit.py - Phabricator API ------------*- python -*--========#
#
# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# ==------------------------------------------------------------------------==#

"""Helper library for interacting with Phabricator.

The Conduit API is Phabricator's HTTPS-accessible API. There is an API browser
interface for the methods:

    https://reviews.llvm.org/conduit/

The APIs are generally close to what is available in the Web UI, but there are
some oddities across versions of Phabricator, and some Conduit calls are marked
as "Frozen" (i.e., "deprecated") but are still necessary.

Most of the logic below is gleaned from the API browser (above) and the output
of `arc --verbose`. (Some is by trial-and-error, too.) The major caveat is that
this is only based on certain versions of Phabricator and Arcanist. (That means
this may need changes in the future, as LLVM's Phabricator instance is
upgraded.)

This module is implemented using only Python standard library modules. This
ensures that this library can be used with (hopefully) zero setup.
"""

import base64
import collections.abc
import contextlib
import http.client
import io
import json
import math
import os
import random
import re
import ssl
import subprocess
import sys
import urllib


################################################################################
# Exception types
################################################################################


class Error(Exception):
    """An exception raised by phabricator integration."""


class ConnectionError(Error):
    """An error trying to communicate with Phabricator."""


class ConduitError(Error):
    """An error returned from the conduit API."""


################################################################################
# Phabricator HTTPS connection
################################################################################


@contextlib.contextmanager
def connect_repository(arcrc, arcconfig, ssl_ctx=None):
    """Connects to Phabricator based on .arcrc and .arcconfig.

    Args:
        arcrc: the user's parsed .arcrc file.
        arcconfig: the repo's parsed .arcconfig file.
        ssl_ctx: (optional) a pre-constructed SSLContext.

    Yields:
        An API object, which is connected to Phabricator.
    """

    # Read this repository's arcconfig to find the Conduit address:
    if 'phabricator.uri' in arcconfig:
        conduit = arcconfig['phabricator.uri']
    elif 'conduit_uri' in arcconfig:
        conduit = arcconfig['conduit_uri']
    else:
        raise Error('.arcconfig did not have a conduit URL')
    conduit = conduit + 'api/'

    # Get the callsign, too:
    if 'repository.callsign' not in arcconfig:
        raise Error('.arcconfig did not have a repo callsign')
    callsign = arcconfig['repository.callsign']

    # Now look for the conduit URI in the user's .arcrc to find the API token:
    for host, options in arcrc.get('hosts', {}).items():
        if host == conduit and 'token' in options:
            api_token = options['token']
            break
    else:
        raise Error('API token not found in .arcrc', conduit)

    url = urllib.parse.urlparse(conduit)
    conn = http.client.HTTPSConnection(url.netloc, url.port, timeout=300,
                                       context=ssl_ctx)
    conn.connect()
    try:
        yield API(url, conn, api_token, callsign)
    finally:
        conn.close()


################################################################################
# Phabricator API utilities
################################################################################


def _conduit_quote(p, prefix=None):
    """Simple recursive encoding in the format expected by Conduit.

    Conduit requests use PHP-style parameter encoding instead of standard URL
    encoding. For example, the parameters:
        {'param': {'dict_key': ['v1', 'v2']}}
    would be:
        param[dict_key][0]=v1
        param[dict_key][1]=v2
    (Which then need to be URL-encoded.)

    Args:
        p: the parameter to encode (probably should be a dict)
        prefix: the prefix to this object.

    Yields:
        (key, value) pairs of the query params to send.
    """
    if isinstance(p, collections.abc.Mapping):
        for key in p:
            if prefix is None:
                new_prefix = key
            else:
                new_prefix = '%s[%s]' % (prefix, key)
            for k, v in _conduit_quote(p[key], new_prefix):
                yield k, v
    elif isinstance(p, str):
        yield prefix, p
    elif isinstance(p, collections.abc.Iterable):
        for i, value in enumerate(p):
            if prefix is None:
                new_prefix = str(i)
            else:
                new_prefix = '%s[%d]' % (prefix, i)
            for k, v in _conduit_quote(value, new_prefix):
                yield k, v
    else:
        yield prefix, p


################################################################################
# Phabricator API
################################################################################


class API:
    """Wrapper for Phabricator's Conduit API.

    The Conduit API is the HTTP(S)-based interface to Phabricator. Its methods
    can be viewed on the Phabricator installation. For LLVM, see:

        https://reviews.llvm.org/conduit/

    The methods in this class are generally Python wrappers around formatting
    the request, and checking for errors in the reply.

    This class is not meant to be a comprehensive Conduit API wrapper. There are
    other Python libraries for that; rather, this class is only meant to support
    what is needed to send diffs to LLVM's Phabricator instance, using only the
    Python standard library.
    """

    def __init__(self, url, conn, api_token, callsign):
        """Initializer.

        Args:
            url: the base URL.
            conn: the connected HTTPSConnection.
            api_token: the user's Conduit API token.
            callsign: the repository callsign that will be used.
        """
        self._url = url
        self._conn = conn
        self._api_token = api_token
        self._repo = self.lookup_repo(callsign)

    def _query(self, method, params):
        """Sends a query to Conduit.

        Args:
            method: the Conduit method name, like 'conduit.query'.
            params: a dict of the parameters to send.

        Returns:
            The response, parsed from JSON.

        Raises:
            ConnectionError: if the HTTP(S) response was not OK.
            ConduitError: if the response had an error_code.
        """
        # Prepare and send the request:
        method_url = urllib.parse.urljoin(self._url.geturl(), method)
        params['api.token'] = self._api_token
        body = urllib.parse.urlencode(list(_conduit_quote(params))).encode()

        # The server (or an intermediate node) may disconnect; say, if the
        # connection was idle for too long. We'll make a couple of retries, with
        # exponential backoff so we don't hammer the server.
        MAX_ATTEMPTS = 5
        for attempt in range(MAX_ATTEMPTS):
            if attempt > 0:
                # Back off a bit. Use a value somewhere around:
                #   e ^ ((attempt-1) / 2)
                # So attempt==1 ~> 1.0s
                #    attempt==2 ~> 1.6s
                #    attempt==3 ~> 2.7s
                #    attempt==4 ~> 4.5s
                delay = math.exp((attempt - 1.5 + random.random()) / 2)
            self._conn.request('POST', method_url, body=body)
            try:
                http_resp = self._conn.getresponse()
            except http.client.RemoteDisconnected as e:
                # After Python 3.5, the call to `connect` is no longer needed.
                # https://docs.python.org/3/library/http.client.html#http.client.HTTPConnection.getresponse
                self._conn.connect()
                continue
            else:
                break
        else:
            raise ConnectionError('Failed %d times' % MAX_ATTEMPTS) from e

        # Check the reply for errors.
        if http_resp.status != http.client.OK:
            raise ConnectionError('HTTP error', http_resp.status)

        resp = json.load(http_resp)
        if resp['error_code']:
            raise ConduitError('Phabricator API error', resp['error_code'],
                               resp.get('error_info', '<no info>'))

        # The result was OK: return it.
        return resp['result']

    def lookup_repo(self, callsign):
        """Returns information for the repository with the given callsign.

        Args:
            callsign: the repository callsign.

        Returns:
            A dict, like:
            {
                'id': 123,
                'type': 'REPO',
                'phid': 'PHID-REPO-abc',
                'fields': {
                    'name': 'Repo Long Display Name',
                    'vcs': 'git',
                    'callsign': 'X',
                    'shortName': 'short-name',
                },
            }
        """
        response = self._query(
            'diffusion.repository.search',
            params={
                'queryKey': 'active',
                'constraints': {'callsigns': [callsign]},
            },
        )
        results = response['data']
        if len(results) != 1:
            raise Error('Expected 1 repo for callsign', callsign, len(results))
        return results[0]

    def lookup_revision_by_hash(self, commit, tree):
        """Search for a revision by commit or tree hash."""
        hashes = []
        if commit: hashes.append(['gtcm', commit])
        if tree:   hashes.append(['gttr', tree])
        response = self._query('differential.query', {'commitHashes': hashes})
        if not response:
            return None
        return response[-1]

    def lookup_revision(self, revision):
        """Search for a revision by ID."""
        response = self._query('differential.query', {'ids': [revision]})
        if not response:
            return None
        return response[-1]

    def create_raw_diff(self, diff):
        """Creates a raw diff from unified diff output.

        `base` is the commit for this diff, like:
        git rev-parse HEAD

        `branch` is the local branch name, as reported by:
        git rev-parse --abbrev-ref --symbolic-full-name 'HEAD'

        `onto` is the remote branch name, as reported by:
        git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'

        Args:
            diff: unified diff output.

        Returns:
            A dict, like:
            {
                'phid': 'PHID-DIFF-abc',
                'id': 123,
                'uri': 'https://reviews.llvm.org/differential/diff/123/',
            }
        """
        params = {
            'repositoryPHID': self._repo['phid'],
            'diff': diff,
        }
        diff_info = self._query('differential.createrawdiff', params)
        return diff_info

    def update_diff_local_commits(self, diff_id, local_commits):
        """Updates local commit information on the diff.

        The dicts for `local_commits` should have some or all of these fields:

            commit      - the hash of the commit object
            tree        - the hash of the tree object
            parents     - the hash of parent commits
            time        - timestamp of the commit
            author      - author's name
            authorEmail - author's email address
            summary     - one-line summary
            message     - longer description

        (See local_workspace.py for helpers.)

        Args:
            diff_id: the id of the diff.
            local_commits: a list of CommitInfos.

        Returns:
            API response.
        """
        params = {
            'diff_id': diff_id,
            'name': 'local:commits',
            'data': {
                info.commit: dict(info._asdict())
                for info in local_commits
            },
        }
        # Fix up the parents.
        for data in params['data'].values():
            data['parents'] = data['parents'].split()
        result = self._query('differential.setdiffproperty', params)

    def get_commit_message(self, title=None, summary=None, revision_id=None,
                           fields=None):
        """Returns a commit message with details for the given revision.

        Args:
            title: the title for the revision.
            summary: the summary for the revision.
            revision_id: get the template for the given revision.
            fields: the full set of fields, as from `parse_commit_message`.

        Returns:
            A string, the new commit message with Phabricator details filled in.
            This can be used, for example, to amend a commit.
        """
        params = {}
        if fields is not None:
            params['fields'] = fields
        if title is not None:
            params.setdefault('fields', {})['title'] = title
        if summary is not None:
            params.setdefault('fields', {})['summary'] = summary
        if revision_id is not None:
            params['revision_id'] = revision_id
        else:
            params['edit'] = 'create'

        return self._query(
            'differential.getcommitmessage',
            params,
        )

    def parse_commit_message(self, commit_message):
        """Parses `commit_message` for Differential fields and returns them.

        Phabricator looks for certain tags in the commit message, for example,
        to auto-close the differential when it is committed. The logic is fairly
        straightforward in the obvious cases, but using the API call ensures we
        match the expected behavior precisely.
        """
        result = self._query(
            'differential.parsecommitmessage',
            {'corpus': commit_message, 'partial': True},
        )
        return result['fields']

    def edit_revision(self, diff_phid, commit_message=None, revid=None,
                      comment=None):
        """Updates (or creates) a differential revision with the given diff.

        If a new revision is created, you may need to amend the local commit
        message to reflect `get_commit_message`. Otherwise, the revision may not
        be closed automatically on commit.

        Example:

            edit_revision(diff_phid='PHID-DIFF-abc',
                          commit_message=textwrap.dedent('''\\
                Revision title

                Revision summary.

                Differential Revision: https://foo/D234''')

        This example updates revision D234. The diff will be set to
        PHID-DIFF-abc, and the title and description will be updated.

        Example:

            edit_revision(revid=234, diff_phid='PHID-DIFF-abc')

        This example updates revision D234. The diff will be set to
        PHID-DIFF-abc, and the title and description will not be changed.

        Args:
            diffid: the 'id' returned from `create_raw_diff`.
            commit_message: optional string, the commit message to use. If it
                has a revision ID tagged, then `revid` is not needed.
            revid: optional int, the revision ID.
            comment: optional string, a comment to attach to the update.

        Returns:
            The revision object, which is a dict like:
            {
                'id': 234,
                'phid': 'PHID-DREV-abc',
            }
        """
        if revid is None and commit_message is None:
            raise Error('Need a commit message for new revisions')

        params = {
            'transactions': [
                {'type': 'update', 'value': diff_phid},
            ],
        }

        if commit_message is not None:
            parsed_message = self.parse_commit_message(commit_message)
            params['transactions'].extend([
                {'type': 'title',   'value': parsed_message['title']},
                {'type': 'summary', 'value': parsed_message['summary']},
            ])
            if revid is None:
                revid = parsed_message.get('revisionID', None)

        if revid is not None:
            params['objectIdentifier'] = revid

        if comment:
            params['transactions'].append({'type': 'comment', 'value': comment})

        result = self._query('differential.revision.edit', params)
        return result['object']
