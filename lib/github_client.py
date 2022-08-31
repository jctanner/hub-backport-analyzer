#!/usr/bin/env python

import json
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


class GithubRepo:
    def __init__(self, org_name, repo_name, client=None):
        self._client = client
        self.org_name = org_name
        self.repo_name = repo_name

    @property
    def branch_names(self):
        api_url = f'https://api.github.com/repos/{self.org_name}/{self.repo_name}/branches'
        ds = self._client.get(api_url)
        return [x['name'] for x in ds]


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
    def repo(self):
        return GithubRepo(self.org_name, self.repo_name, client=self._client)

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
    def merge_commit_sha(self):
        return self.raw.get('merge_commit_sha')

    @property
    def merge_commit_branches(self):
        '''Return a list of branches the merge commit made it into'''

        """
                csha = pr.raw['merge_commit_sha']
                branches = self.gc.get_commit_branches(pr.org_name, pr.repo_name, csha)
                branches = [x.replace('stable-', '') for x in branches if x.startswith('stable-')]
                branches.append(dev_version)
                branches = sorted(set(branches))
        """

        mc_sha = self.merge_commit_sha
        mc_url = f'https://api.github.com/repos/{self.org_name}/{self.repo_name}/commits/{mc_sha}'
        mc_ds = self._client.get(mc_url)

        # https://stackoverflow.com/a/16782303
        # https://api.github.com/repos/twitter/bootstrap/commits?sha=3.0.0-wip
        repo_branches = self.repo.branch_names[:]
        repo_branches = [x for x in repo_branches if 'dependabot' not in x]
        repo_branches = [x for x in repo_branches if 'patchback' not in x]

        found = []
        for rb in repo_branches:
            bcurl = f'https://api.github.com/repos/{self.org_name}/{self.repo_name}/commits?sha={rb}'
            logger.debug(f'paginate {bcurl}')
            branch_commits = self._client.paginated_get(bcurl)
            branch_shas = [x['sha'] for x in branch_commits]
            if mc_sha in branch_shas:
                found.append(rb)

        found = sorted(set(found))
        return found

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

        # cross references
        for event in self.timeline:
            if event['event'] != 'cross-referenced':
                continue
            iurl = event['source']['issue']['html_url']
            purl = iurl.replace('/issue/', '/pull/')

            if repo_url_from_html_url(self.html_url) != repo_url_from_html_url(purl):
                continue

            blinks.append(purl)

        # references
        for event in self.timeline:
            if event['event'] != 'referenced':
                continue

            if 'commit_url' not in event:
                continue

            commit = self._client.get(event['commit_url'])
            commit_pulls = self._client.get(event['commit_url'] + '/pulls')
            for cp in commit_pulls:

                purl = cp['html_url']
                if purl in blinks:
                    continue

                logger.debug('\t\t\t' + purl)

                if repo_url_from_html_url(self.html_url) != repo_url_from_html_url(purl):
                    continue

                # is actually related?
                ds = self._client.get(cp['url'])
                if 'backport' in ds['title'].lower() and str(self.number) in ds['title']:
                    logger.debug(f"\t\t\t\ttitle is related: {ds['title']}")
                    blinks.append(purl)
                    continue

                # is this a cherry pick?
                if ds.get('merge_commit_sha'):
                    mc_url = event['commit_url'].split('/')
                    mc_url[-1] = ds['merge_commit_sha']
                    mc_url = '/'.join(mc_url)
                    mc_ds = self._client.get(mc_url)

                    if not 'cherry' in mc_ds['commit']['message']:
                        logger.debug('\t\t\t\tis not a cherry pick')
                        continue

                    msg = mc_ds['commit']['message']
                    msg = msg.split('\n')
                    cp_commit = msg[-1]
                    cp_commit = cp_commit.replace('(', '').replace(')', '')
                    cp_commit = cp_commit.split()[-1]

                    if cp_commit == self.merge_commit_sha:
                        logger.debug(f"\t\t\t\tcherry-pick: {ds['title']}")
                        blinks.append(purl)
                        continue

                #import epdb; epdb.st()
                #blinks.append(purl)

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
            # logger.debug(f'GET {next_url}')
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

    def get_commit_tags(self, org, repo, commit):
        # api_url = f'https://api.github.com/repos/{org}/{repo}/commits/{commit}'
        # ds = self.get(api_url)

        checkout_dir = self.make_checkout(org, repo)
        fn = os.path.dirname(checkout_dir)
        fn = os.path.join(fn, f'{org}_{repo}_tag_commit_map.json')
        if os.path.exists(fn):
            with open(fn, 'r') as f:
                commit_map = json.loads(f.read())
        else:
            pid = subprocess.run('git tag -l', shell=True, cwd=checkout_dir, stdout=subprocess.PIPE)
            tag_names = pid.stdout.decode('utf-8').split('\n')
            tag_names = [x.strip() for x in tag_names if x.strip()]

            commit_map = {}
            for tn in tag_names:
                cmd = f'git log {tn} --format="%H %s"'
                logger.debug(cmd)
                pid = subprocess.run(cmd, shell=True, cwd=checkout_dir, stdout=subprocess.PIPE)
                if pid.returncode != 0:
                    continue
                loglines = pid.stdout.decode('utf-8').split('\n')
                loglines = [x.strip() for x in loglines if x.strip()]
                for logline in loglines:
                    sha = logline.split()[0]
                    if sha not in commit_map:
                        commit_map[sha] = []
                    commit_map[sha].append(tn)

            with open(fn, 'w') as f:
                f.write(json.dumps(commit_map))

        return commit_map.get(commit, [])

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
