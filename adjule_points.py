#!/usr/bin/env python3

import argparse
import copy
import getpass
import logging
import os
import re
import shutil
import sys
import time
from string import Template

import dateparser
import progressbar
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

progressbar.streams.wrap_stderr()
logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)
logger = logging.getLogger('__name__')


class Problem():
    def __init__(self,
                 tag,
                 deadline,
                 languages,
                 name='',
                 points=0,
                 manual=False):
        """manual - manually edited problem"""
        self.tag = tag
        self.deadline = deadline
        self.languages = languages
        self.name = name
        self.points = points
        self.manual = manual


class Student():
    def __init__(self, nick, name='brak', number='brak', problems=None):
        "docstring"
        self.name = name
        self.nick = nick
        self.number = number
        if problems == None:
            self.problems = []

    def find_problem_by_tag(self, problem_tag):
        for p in self.problems:
            if p.tag == problem_tag:
                return p


class AdjuleManager():
    def __init__(self, driver, login, group, problems_path, marks_path):
        "docstring"
        self.driver = driver
        self.login = login
        self.group_url = f'https://adjule.pl/groups/{group}'
        self.driver.implicitly_wait(1)
        self.problems = self.load_problems(problems_path)
        self.marks_path = marks_path
        self.students = []
        self.submission_template = Template(
            'https://adjule.pl/submissions?user=${user}&problem=${problem_tag}&page=${page_nr}'
        )

    def load_problems(self, problems_path):
        problems = []
        with open(problems_path) as f:
            for line in f:
                problem_tag, deadline, languages = line.rstrip('\n').split(
                    '\t')
                deadline = dateparser.parse(f'{deadline} 23:59:59.999999',
                                            ['%d.%m.%y %H:%M:%S.%f'])
                languages = [l.replace(' ', '') for l in languages.split(',')]
                problem = Problem(problem_tag, deadline, languages)
                problems.append(problem)
        return problems

    def update_problem_names(self):
        logging.info('Gathering problem names...')
        for problem in progressbar.progressbar(self.problems):
            self.driver.get(f'{self.group_url}/problems/{problem.tag}')
            while not problem.name:
                problem.name = self.driver.find_element_by_css_selector(
                    'div.small-6.cell.card-section > h1').text
                time.sleep(1)

    def log_in(self):
        logger.info('Loading logging page...')
        self.driver.get('https://adjule.pl/#login')
        got_it_button = self.driver.find_element_by_css_selector(
            'button.button.expanded.hollow.success')
        if got_it_button:
            got_it_button.click()  #if there is cookie modal, click it
        username = self.driver.find_element_by_name("login")
        password = self.driver.find_element_by_name("password")

        username.send_keys(self.login)
        password.send_keys(
            getpass.getpass(f'\nPassword for {self.login} in adjule:'))

        actions = ActionChains(self.driver)
        actions.send_keys(Keys.ENTER)
        logger.info('Logging in...')
        actions.perform()

    def get_students_urls_on_page(self):
        profile_elements = self.driver.find_elements_by_css_selector(
            'table.ranking.relative td > a[href^="/profile/"]')
        return [p.get_attribute('href') for p in profile_elements]

    def get_all_students_urls(self):
        logger.info('Gathering students urls...')
        self.driver.get('{}/ranking'.format(self.group_url))
        students_urls = []
        last_page = False
        while not last_page:
            page_urls = self.get_students_urls_on_page()
            students_urls.extend(page_urls)
            next_button = self.driver.find_element_by_css_selector(
                'li.pagination-next > button')
            if next_button.get_attribute('class') == 'disabled':
                last_page = True
            else:
                next_button.click()
                time.sleep(1)
        students_urls = sorted(
            list(set(students_urls))
        )  #adjule sometimes fuck up, so we make sure students are unique
        logger.info(f'Found {len(students_urls)} students.')
        return students_urls

    def get_student_data(self, student_url):
        self.driver.get(student_url)
        profile_name_element = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                                            'ul.no-bullet a.profilename')))
        profile_name = profile_name_element.text
        profile_name_array = profile_name.split()
        if len(profile_name_array) == 3:
            name = '{} {}'.format(profile_name_array[0], profile_name_array[2])
            nick = profile_name_array[1].strip("'").rstrip("'")
        else:
            nick = profile_name
            name = 'brak'
        student_nr = self.driver.find_element_by_css_selector(
            'ul.no-bullet > li:nth-child(2)').text
        if 'Student' in student_nr:
            student_nr = re.sub('[^0-9]', '', student_nr)
        else:
            student_nr = 'brak'
        return name, nick, student_nr

    def fill_all_students_data(self, students_urls):
        logger.info('Filling students data...')
        for url in progressbar.progressbar(students_urls):
            name, nick, student_nr = self.get_student_data(url)
            self.students.append(Student(nick, name, student_nr))

    def extract_submission_data(self, submission):
        date = submission.find_element_by_css_selector(
            "[data-label='Date']").get_attribute('title')
        # problem_name = submission.find_element_by_css_selector(
        #     "[data-label='Problem']").get_property('textContent')
        language = submission.find_element_by_css_selector(
            "[data-label='Language']").text
        return dateparser.parse(date), language

    def evaluate_problem(self, student, problem):
        problem = copy.copy(problem)
        problem.points = 0
        exist_submission = True
        page_nr = 0
        while exist_submission:
            url = self.submission_template.substitute(
                user=student.nick, problem_tag=problem.tag, page_nr=page_nr)
            self.driver.get(url)
            accepted_submissions = self.driver.find_elements_by_css_selector(
                'table.submissions tr.acc')
            if not accepted_submissions:
                exist_submission = False
            for acc_sub in accepted_submissions:
                date, language = self.extract_submission_data(acc_sub)
                if language.lower() not in problem.languages:
                    continue
                if date < problem.deadline:
                    problem.points = 1
                    return problem
                else:
                    problem.points = 0.5
            page_nr += 1

        return problem

    def add_problems_to_students(self):
        logging.info('Evaluating students...')
        for i, student in enumerate(self.students):
            logger.info(
                "Evaluating student ({}/{}): name='{}', nick='{}', number='{}'."
                .format(i + 1, len(self.students), student.name, student.nick,
                        student.number))
            for problem in progressbar.progressbar(self.problems):
                student.problems.append(
                    self.evaluate_problem(student, problem))

    def find_student_by_nick(self, nick):
        for student in self.students:
            if student.nick == nick:
                return student

    def update_student_problems_with_manual_marks(self):
        logger.info('Updating students with manual marks...')
        with open(self.marks_path) as f:
            header = next(f).rstrip('\n').split('\t')
            for i, val in enumerate(header):
                problem_tag = re.search('[^(]+ \((.+)\)$', val)
                if problem_tag:
                    header[i] = problem_tag.group(1)
            for line in f:
                row = line.rstrip('\n').split('\t')
                nick = row[header.index('Nick')]
                student = self.find_student_by_nick(nick)
                for col_nr, val in enumerate(row):
                    if '*' in val:
                        problem_tag = header[col_nr]
                        problem = student.find_problem_by_tag(problem_tag)
                        problem.manual = True
                        problem.points = int(val.rstrip('*'))

    def update_marks(self):
        if os.path.isfile(self.marks_path):
            self.backup_marks()
            self.update_student_problems_with_manual_marks()
        logger.info(f'Creating {self.marks_path}')
        with open(self.marks_path, 'w') as f:
            print('Student\tNick\tNumer indeksu\t', end='', file=f)
            for problem in self.problems:
                print(f'{problem.name} ({problem.tag})\t', end='', file=f)
            print('Suma', file=f)
            for student in self.students:
                suma = 0
                print(
                    f'{student.name}\t{student.nick}\t{student.number}\t',
                    end='',
                    file=f)
                for problem in student.problems:
                    suma += problem.points
                    if problem.manual:
                        print(f'{problem.points}*\t', end='', file=f)
                    else:
                        print(f'{problem.points}\t', end='', file=f)
                print(suma, file=f)

    def backup_marks(self, marks_backup_path=None):
        if marks_backup_path == None:
            marks_backup_path = f'{self.marks_path}.bak'
        shutil.copy(self.marks_path, marks_backup_path)
        logger.info(f'Backed up {self.marks_path} to {self.marks_path}.bak')

    def run(self):
        self.log_in()
        start = time.time()
        self.update_problem_names()
        students_urls = self.get_all_students_urls()
        self.fill_all_students_data(students_urls)
        self.add_problems_to_students()
        self.update_marks()
        end = time.time()
        total_time = round(end - start)
        logger.info(f'Total execution time: {total_time}s')


def get_args():
    parser = argparse.ArgumentParser(
        description='Script for managing students in adjule.pl',
        epilog=
        """example usage: ./adjule_points.py --login siulkilulki --tasks zadania.tsv --marks oceny-1ca.tsv --group ppr1ca2018
NOTE: Script will prompt for adjule password (on command-line) after reaching log in page.""",
        formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument('--login', required=True, help='Login to adjule.pl')
    parser.add_argument(
        '--tasks',
        required=True,
        help=
        """Path to tsv file with following columns <problem_tag> <deadline> <allowed_languages>.
    <problem_tag> - problem tag from adjule
    <deadline> - date in form of dd.mm.yy
    <allowed_languages> - comma separeted languages written with lowercase
Example tasks file:
ppr10\t11.11.18\tc,c++,python
ppr12\t21.12.18\tc""")
    parser.add_argument(
        '--marks',
        required=True,
        help="""File with student marks. If it doesn\'t exist it will be created.
""")
    parser.add_argument(
        '--group',
        required=True,
        help='Adjule group written with lowercase e.g. ppr1ca2018')
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Log debug information. Default: Don\'t log debug info.')
    # headless_parser = parser.add_mutually_exclusive_group(required=False)
    # headless_parser.add_argument(
    #     '--headless',
    #     dest='headless',
    #     action='store_true',
    # help='Runs browser in headless mode, without GUI).')
    parser.add_argument(
        '--no-headless',
        dest='headless',
        action='store_false',
        help=
        'Runs real browser. Default: Runs browser in headless mode (faster).')
    parser.set_defaults(debug=False, headless=True)
    return parser.parse_args()


def main():
    args = get_args()
    if not args.debug:
        logging.getLogger().setLevel(logging.INFO)
    chrome_options = Options()
    chrome_options.headless = args.headless
    driver = webdriver.Chrome(options=chrome_options)

    adjule_manager = AdjuleManager(driver, args.login, args.group, args.tasks,
                                   args.marks)
    adjule_manager.run()
    driver.quit()


if __name__ == '__main__':
    main()
