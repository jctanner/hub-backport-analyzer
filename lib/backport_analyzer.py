import argparse
import copy
import json
import os
import time

from pprint import pprint
from logzero import logger

from lib.github_client import GithubClient



def fixversion_to_backport_name(fv_name):
    # 4.3.6 -> 4.3

    if fv_name.startswith('cloud'):
        return None

    try:
        fv = fv_name.split()[0]
        fv = fv.replace('cloud', '.')
        fvparts = fv.split('.')
        bpv = fvparts[0] + '.' + fvparts[1]
    except Exception as e:
        #logger.exception(e)
        #print(fv_name)
        #import epdb; epdb.st()
        return None
    # import epdb; epdb.st()
    return bpv


class BackportAnalyzer:

    jira_issues = None
    cachedir = '.data'
    errors = None
    issue_key = None

    def __init__(self, issue=None):
        self.issue_key = issue
        self.gc = GithubClient()
        self.load_jira_issues()
        self.errors = []
        self.jira_states = set()
        self.process_jira_issues()

    def load_jira_issues(self):
        logger.info('load all jira issues')
        with open( os.path.join(self.cachedir, 'jiras.json'), 'r') as f:
            self.jira_issues = json.loads(f.read())

        self.jira_issues = sorted(self.jira_issues, reverse=True, key=lambda x: int(x['key'].replace('AAH-', '')))

        if self.issue_key:
            self.jira_issues = [x for x in self.jira_issues if x['key'] == self.issue_key]

    def process_jira_issues(self):
        for issue in self.jira_issues:
            self.process_jira_issue(issue)

        logger.info("--------------- RESULTS ----------------")
        for error in self.errors:
            logger.error(error)

    def process_jira_issue(self, issue):
        ikey = issue['key']
        istate = issue['fields']['status']['name'].lower()
        self.jira_states.add(istate)

        fvs = issue['fields'].get('fixVersions')
        if not fvs:
            return

        if 'customfield_12310220' not in issue['fields']:
            return

        pr_urls = issue['fields']['customfield_12310220']
        if not pr_urls:
            return

        logger.info(ikey)
        for fv in fvs:
            logger.info('\tFIXVERSION: ' + fv['name'])
        backports_expected = [fixversion_to_backport_name(x['name']) for x in fvs]
        backports_expected = [x for x in backports_expected if x]
        backports_expected = sorted(set(backports_expected))

        for pr_url in pr_urls:

            if 'github' not in pr_url:
                continue

            if 'galaxy' not in pr_url and 'hub-ui' not in pr_url:
                continue

            if 'importer' in pr_url:
                continue

            try:
                pr = self.gc.get_pullrequest(pr_url)
            except Exception as e:
                logger.error(f'\tcould not find {pr_url}')
                continue

            # find the new PR if this one was deprecated
            swapped = False
            if not pr.merged and pr.closed:
                slinks = pr.successor_links
                if not slinks:
                    self.errors.append(
                        f'{ikey} links to {pr.html_url} [{pr.author}]'
                        + ' which was closed without merge'
                    )
                    continue

                candidates = []
                for slink in slinks:
                    _pr = self.gc.get_pullrequest(slink)
                    candidates.append(_pr)

                if len(candidates) > 1:
                    self.errors.append(
                        f'{ikey} links to {pr.html_url} [{pr.author}]'
                        + ' which was closed without merge and has multiple successors'
                    )
                    continue

                new_pr = candidates[0]
                self.errors.append(
                    f'{ikey} links to {pr.html_url} [{pr.author}]'
                    + f' which was deprecated by {new_pr.html_url} [{pr.author}]'
                )
                pr = new_pr
                swapped = True

            done_states = ['done', 'ready for qa', 'in qa']
            if istate in done_states and not pr.merged:
                self.errors.append(
                    f'{ikey} is marked as "{istate}" when'
                    + f' {pr.html_url} [{pr.author}] is not merged'
                )

            merge_state = 'MERGED' if pr.merged else 'NOT MERGED'

            logger.info('\t' + pr_url + ' ' + pr.branch_name + ' ' + merge_state)
            labels = pr.label_names

            backport_requests = [x.replace('backport-', '') for x in labels if x.startswith('backport-')]
            backported_to = [x.replace('backported-', '') for x in labels if x.startswith('backported-')]
            backports_failed = [x for x in backport_requests if x not in backported_to]
            backports_missed = [x for x in backports_expected if x not in backport_requests]

            # what is the dev version?
            dev_version = self.gc.get_dev_branch_version(pr.org_name, pr.repo_name)
            dev_version = fixversion_to_backport_name(dev_version)

            # what branches did this commit end up in?
            branches = []
            if pr.merged:
                csha = pr.raw['merge_commit_sha']
                branches = self.gc.get_commit_branches(pr.org_name, pr.repo_name, csha)
                branches = [x.replace('stable-', '') for x in branches if x.startswith('stable-')]
                branches.append(dev_version)
                branches = sorted(set(branches))
            if backports_expected and branches:
                missing_branches = [x for x in backports_expected if x not in branches]
                if not missing_branches:
                    continue

            # if missing_branches:
            #     import epdb; epdb.st()

            default_ds = {
                'ikey': ikey,
                'pr': None,
                'pr_url': None,
                'pr_branch': None,
                'merged': None,
                'fv': None,
                'fv_marked': None
            }

            bpmap = {}

            # find the backport comments
            blinks = pr.backport_links
            if pr.branch_name.startswith('stable-'):
                blinks.append(pr.html_url)
                blinks = sorted(set(blinks))
            for blink in blinks:
                bp_pr = self.gc.get_pullrequest(blink)
                bn = bp_pr.branch_name
                bv = bn.replace('stable-', '')

                if bv not in bpmap:
                    bpmap[bv] = []

                ds = copy.deepcopy(default_ds)
                ds['pr'] = bp_pr
                ds['pr_url'] = bp_pr.html_url
                ds['pr_branch'] = bp_pr.branch_name
                ds['merged'] = bp_pr.merged
                ds['fv'] = bv
                ds['fv_marked'] = bv in backports_expected
                bpmap[bv].append(ds)

            all_versions = sorted(set(backports_expected + backport_requests))
            for avs in all_versions:
                if avs not in bpmap:
                    if avs in backports_expected and avs != dev_version:
                        self.errors.append(
                            f'{ikey} has a fix version of {avs}'
                            + f' but no related backport PR for {pr.html_url} [{pr.author}]'
                        )
                    continue
                if avs in bpmap and avs not in backports_expected:
                    avs_pr = bpmap[avs][0]['pr']
                    self.errors.append(
                        f'{ikey} has no fix version for {avs}'
                        + f' but was backported to {avs} in {avs_pr.html_url}'
                    )
                if pr.merged and backports_expected and istate in done_states:
                    merged_backports = [x for x in bpmap[avs] if x['merged']]
                    if not merged_backports:
                        self.errors.append(
                            f'{ikey} has no merged backports for {pr.html_url} [{pr.author}]'
                            + f' to {avs} but is in a "done" state'
                        )



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--issue')
    args = parser.parse_args()
    BackportAnalyzer(issue=args.issue)
