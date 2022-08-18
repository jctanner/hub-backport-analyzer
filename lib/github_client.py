#!/usr/bin/env python

import os
import requests
import requests_cache
import subprocess
from logzero import logger


requests_cache.install_cache('github_cache')


def convert_html_url_to_api_url(html_url):
    # https://github.com/ansible/galaxy_ng/pull/1370
    # https://api.github.com/repos/OWNER/REPO/issues
    if 'api.github.com' in html_url:
        return html_url

    api_url = html_url.replace('github.com', 'api.github.com/repos')
    api_url = api_url.replace('/pull/', '/pulls/')
    return api_url


def repo_url_from_html_url(html_url):
    # https://github.com/ansible/galaxy_ng/pull/1
    #   to
    # https://github.com/ansible/galaxy_ng
    parts = html_url.split('/')
    return '/'.join(parts[:5])


class GithubPullRequest:

    def __init__(self, raw, client=None):
        self._client = client
        self.raw = raw

    def __repr__(self):
        return f'<GithubPullRequest {self.html_url}>'

    @property
    def html_url(self):
        return self.raw.get('html_url')

    @property
    def org_name(self):
        return self.raw['url'].split('/')[4]

    @property
    def repo_name(self):
        return self.raw['url'].split('/')[5]

    @property
    def number(self):
        return self.raw['number']

    @property
    def author(self):
        return self.raw['user']['login']

    @property
    def state(self):
        return self.raw.get('state')

    @property
    def closed(self):
        return self.raw.get('state') == 'closed'

    @property
    def merged(self):
        return self.raw.get('merged')

    @property
    def label_names(self):
        return [x['name'] for x in self.raw['labels']]

    @property
    def branch_name(self):
        if 'base' not in self.raw:
            import epdb; epdb.st()
        return self.raw['base']['ref']

    @property
    def comments(self):
        comments_url = self.raw['_links']['comments']['href']
        comments = self._client.paginated_get(comments_url)
        return comments

    @property
    def timeline(self):
        api_url = self.raw['url'].replace('pulls', 'issues') + '/timeline'
        return self._client.paginated_get(api_url)

    @property
    def backport_links(self):
        blinks = []
        for comment in self.comments:
            if 'patch' not in comment['user']['login']:
                continue
            if 'backported as' not in comment['body'].lower():
                continue
            for line in comment['body'].split('\n'):
                if not line.startswith('Backported as'):
                    continue
                blink = line.strip().split()[-1]
                blinks.append(blink)
                break

        for event in self.timeline:
            if event['event'] != 'cross-referenced':
                continue
            iurl = event['source']['issue']['html_url']
            purl = iurl.replace('/issue/', '/pull/')

            if repo_url_from_html_url(self.html_url) != repo_url_from_html_url(purl):
                continue

            blinks.append(purl)

        blinks = sorted(set(blinks))
        return blinks

    @property
    def successor_links(self):

        matchers = [
            'reopening this here',
            'deprecated by',
            'in favor of',
            'in favour of'
        ]

        slinks = []
        for comment in self.comments:
            for line in comment['body'].split('\n'):
                if 'github.com' not in line:
                    continue

                matched = False
                for matcher in matchers:
                    if matcher in line.lower():
                        matched = True
                        break
                if not matched:
                    # import epdb; epdb.st()
                    continue

                words = line.lower().split()
                for word in words:
                    if 'github.com' in word:
                        slinks.append(word)

        return slinks


class GithubClient:

    checkouts = None

    def __init__(self):
        self.token = os.environ.get('GITHUB_TOKEN')
        if self.token is None:
            raise Exception('GITHUB_TOKEN must be exported!')
        self.checkouts = {}

    @property
    def headers(self):
        return {
            'Authorization': f'token {self.token}'
        }

    def get(self, api_url):
        # logger.info(f'GET {api_url}')
        rr = requests.get(api_url, headers=self.headers)
        return rr.json()

    def paginated_get(self, next_url):
        data = []
        while next_url:
            # logger.info(f'GET {next_url}')
            rr = requests.get(next_url, headers=self.headers)
            ds = rr.json()
            data.extend(ds)

            if not rr.links:
                break

            if not rr.links.get('next', {}).get('url'):
                break

            next_url = rr.links.get('next', {}).get('url')

        return data

    def _convert_html_url_to_api_url(self, html_url):
        # https://github.com/ansible/galaxy_ng/pull/1370
        # https://api.github.com/repos/OWNER/REPO/issues
        if 'api.github.com' in html_url:
            return html_url

        api_url = html_url.replace('github.com', 'api.github.com/repos')
        return api_url

    def get_pullrequest(self, issue_url):
        api_url = convert_html_url_to_api_url(issue_url)
        ds = self.get(api_url)
        if ds.get('message') == 'Not Found':
            raise Exception(f'PR not found {api_url}')
        return GithubPullRequest(ds, client=self)

    def get_dev_branch_version(self, org, repo):
        checkout_dir = self.make_checkout(org, repo)
        setup_fn = os.path.join(checkout_dir, 'setup.py')
        if os.path.exists(setup_fn):
            with open(setup_fn, 'r') as f:
                fdata = f.read()
            flines = fdata.split('\n')
            flines = [x for x in flines if x.startswith('version =')]
            if flines:
                version = flines[0].split()[-1]
                version = version.replace('"', '')
                version = version.replace("'", '')
                return version

        if repo == 'ansible-hub-ui':
            setup_fn = os.path.join(checkout_dir, 'ansible-hub-ui', '__init__.py')
            with open(setup_fn, 'r') as f:
                fdata = f.read()
            flines = fdata.split('\n')
            flines = [x for x in flines if '__version__' in x]
            if flines:
                version = flines[0].split()[-1]
                version = version.replace('"', '')
                version = version.replace("'", '')
                return version

        if repo == 'galaxy':
            pid = subprocess.run(
                'git describe --always --match v*',
                shell=True,
                cwd=checkout_dir,
                stdout=subprocess.PIPE
            )
            version = pid.stdout.decode('utf-8').strip()
            if '-' in version:
                chunks = version.lstrip('v').rsplit('-', 2)
                return '{0}.dev{1}+{2}'.format(*chunks)

            if '.' in version:
                return version.lstrip('v')

            return '0.0.0.dev0+{0}'.format('v')

        import epdb; epdb.st()

    def get_commit_branches(self, org, repo, commit):
        # api_url = f'https://api.github.com/repos/{org}/{repo}/commits/{commit}'
        # ds = self.get(api_url)

        checkout_dir = self.make_checkout(org, repo)
        cmd = f'git branch -a --contains {commit}'
        pid = subprocess.run(cmd, shell=True, cwd=checkout_dir, stdout=subprocess.PIPE)
        branches = pid.stdout.decode('utf-8')
        branches = branches.split('\n')
        branches = [x.strip() for x in branches]
        branches = [x for x in branches if x.startswith('remotes/origin')]
        branches = [x for x in branches if '->' not in x]
        branches = [x.replace('remotes/origin/', '') for x in branches]
        #import epdb; epdb.st()
        return branches

    def make_checkout(self, org, repo):
        tdir = '/tmp/checkouts'
        if not os.path.exists(tdir):
            os.makedirs(tdir)
        checkout_dir = os.path.join(tdir, f'{org}.{repo}')
        if not os.path.exists(checkout_dir):
            clone_url = f'https://github.com/{org}/{repo}'
            cmd = f'git clone {clone_url} {checkout_dir}'
            subprocess.run(cmd, shell=True)
        self.checkouts[(org, repo)] = checkout_dir
        return checkout_dir
