#!/usr/bin/env python3
# Copyright Mycroft AI, Inc. 2018. All Rights Reserved.
import sys
from collections import OrderedDict

import json
import os
import re
from difflib import SequenceMatcher
from git import Repo, Git
from github import Github, GithubException
from github.ContentFile import ContentFile
from github.Repository import Repository
from msk.util import ask_for_github_credentials, register_git_injector
from msm import MycroftSkillsManager, SkillEntry, SkillRepo
from os import isatty
from os.path import dirname, abspath, isfile, join, isdir
from prettyparse import create_parser
from tempfile import gettempdir
from typing import Optional

usage = '''
Generates a JSON containing info on Mycroft Skills in the Skill Repo

:-o --output-file str -
    Output json file

:-u --upload
    Whether to upload result to mycroft-skills-metric
'''

root = dirname(abspath(__file__))


class TempClone:
    def __init__(self, url: str):
        import posixpath
        self.path = join(gettempdir(), posixpath.basename(url))
        if not isdir(self.path):
            Repo.clone_from(url, self.path)
        self.git = Git(self.path)

    def write(self, path: str, content: str):
        with open(join(self.path, path), 'w') as f:
            f.write(content)
        self.git.add(path)
        self.git.commit(amend=True, no_edit=True)
        self.git.push(force=True)


def load_github() -> Github:
    if isfile(join(root, 'token.txt')):
        with open(join(root, 'token.txt')) as f:
            token = f.read().strip()
        register_git_injector(token, '')
        return Github(token)
    elif os.environ.get('GITHUB_TOKEN'):
        token = os.environ['GITHUB_TOKEN']
        register_git_injector(token, '')
        return Github(token)
    elif isatty(sys.stdout.fileno()):
        return ask_for_github_credentials()
    else:
        print('Warning: No authentication. May exceed GitHub rate limit')
        return Github()


def extract_sections(readme_content: str) -> OrderedDict:
    last_section = ''
    sections = OrderedDict({last_section: ''})
    for line in readme_content.split('\n'):
        line = line.strip()
        if line.startswith('#'):
            last_section = line.strip('# ')
            sections[last_section] = ''
        else:
            sections[last_section] += '\n' + line
    for section_name in list(sections):
        sections[section_name] = sections[section_name].strip()
    sections[''] = sections.pop('')  # Shift to end
    return sections


def compare(a: str, b: str) -> float:
    return SequenceMatcher(a=a.lower(), b=b.lower()).ratio()


def norm(x: str) -> str:
    return x.lower().replace('-', ' ')


def find_secton(name: str, sections: dict, min_conf: float = 0.5) -> Optional[str]:
    title, conf = max([(title, compare(title, name)) for title in sections], key=lambda x: x[1])
    return None if conf < min_conf else sections[title]


def parse_whitespace(s: str) -> str:
    s = re.sub(r'(?<!\n)\n(?!\n)', r' ', s)
    s = re.sub(r'\n+', '\n', s)
    s = re.sub(r' +', r' ', s)
    return s


def format_sent(s: str) -> str:
    s = caps(s)
    if s and s[-1].isalnum():
        return s + '.'
    return s


def caps(s: str) -> str:
    """Capitalize first letter without lowercasing the rest"""
    return s[:1].upper() + s[1:]


def parse_example(example: str) -> str:
    example = parse_whitespace(example.strip(' \n"\'`'))
    example = re.split(r'["`]', example)[0]

    # Remove "Hey Mycroft, "
    for prefix in ['hey mycroft', 'mycroft', 'hey-mycroft']:
        if example.lower().startswith(prefix):
            example = example[len(prefix):]
    example = example.strip(' ,')  # Fix ", " from "Hey Mycroft, ..."
    if any(
            example.lower().startswith(word + suffix + ' ')
            for word in ['who', 'what', 'when', 'where']
        for suffix in ["'s", "s", "", "'d", "d" "'re", "re"]
    ):
        example = example.rstrip('?.') + '?'
    example = format_sent(example)
    return example


def find_examples(sections: dict) -> list:
    return re.findall(
        string=find_secton('examples', sections) or find_secton('usage', sections) or '',
        pattern=r'(?<=[-*]).*', flags=re.MULTILINE
    )


def find_title_info(sections: dict, skill_name: str) -> tuple:
    title_section = next(iter(sections))
    if compare(norm(title_section), norm(skill_name)) >= 0.3:
        return caps(title_section), sections[title_section]
    else:
        return norm(skill_name).title(), sections['']


def generate_summary(github: Github, skill_entry: SkillEntry):
    author = skill_entry.extract_author(skill_entry.url)
    repo_name = skill_entry.extract_repo_name(skill_entry.url)
    repo_id = '/'.join([author, repo_name])
    repo = github.get_repo(repo_id)  # type: Repository
    readme_file = repo.get_readme()  # type: ContentFile
    readme = readme_file.decoded_content.decode()
    sections = extract_sections(readme)
    title, short_desc = find_title_info(sections, skill_entry.name)

    return {
        'repo': repo.html_url,
        'title': title,
        'name': skill_entry.name,
        'author': find_secton('author', sections) or caps(skill_entry.author),
        'github_username': skill_entry.author,
        'short_desc': format_sent(parse_whitespace(short_desc.replace('\n', ' '))).rstrip('.'),
        'description': format_sent(parse_whitespace(find_secton('description', sections) or '')),
        'examples': [parse_example(i) for i in find_examples(sections)],
        'requires': parse_whitespace(find_secton('require', sections, 0.9) or '').split(),
        'excludes': parse_whitespace(find_secton('exclude', sections, 0.9) or '').split()
    }


def upload_summaries(github: Github, summaries: dict):
    print('Uploading skill-metadata.json...')
    repo = github.get_repo('MycroftAI/mycroft-skills-data')  # type: Repository
    if not repo.permissions.push:
        print('You don\'t have write permissions')
        exit(1)
    clone = TempClone('https://github.com/mycroftai/mycroft-skills-data')
    clone.write('skill-metadata.json', json.dumps(summaries, indent=4))


def main():
    args = create_parser(usage).parse_args()
    github = load_github()

    summaries = {}
    repo = SkillRepo(path=join(gettempdir(), 'mycroft-skills-repo'))
    for skill_entry in MycroftSkillsManager(repo=repo).list():
        if not skill_entry.url:
            continue
        print('Generating {}...'.format(skill_entry.name))
        try:
            summary = generate_summary(github, skill_entry)
        except GithubException as e:
            print('Failed to generate summary:', repr(e))
            continue
        summaries[skill_entry.name] = summary

    if args.output_file:
        with open(args.output_file, 'w') as f:
            json.dump(summaries, f)
    else:
        print(json.dumps(summaries, indent=4))
    if args.upload:
        upload_summaries(github, summaries)


if __name__ == '__main__':
    main()
