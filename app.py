import sys
if (sys.version_info.major < 3 or sys.version_info.minor < 6):
    print ('Hedy requires Python 3.6 or newer to run. However, your version of Python is', '.'.join ([str (sys.version_info.major), str (sys.version_info.minor), str (sys.version_info.micro)]))
    quit ()

# coding=utf-8
import datetime
import collections
import hedy
import json
import logging
import os
from os import path
import re
import traceback
import uuid
from ruamel import yaml
from flask_commonmark import Commonmark
from werkzeug.urls import url_encode
from config import config
from website.auth import auth_templates, current_user, requires_login, is_admin, is_teacher
from utils import timems, load_yaml, load_yaml_rt, dump_yaml_rt, version, is_debug_mode
import utils

# app.py
from flask import Flask, request, jsonify, session, abort, g, redirect, Response, make_response
from flask_helpers import render_template
from flask_compress import Compress

# Hedy-specific modules
import courses
import hedyweb
from website import querylog, aws_helpers, jsonbin, translating, ab_proxying, cdn, database

# Set the current directory to the root Hedy folder
os.chdir(os.path.join (os.getcwd (), __file__.replace (os.path.basename (__file__), '')))

# Define and load all available language data
ALL_LANGUAGES = {
    'en': 'English',
    'nl': 'Nederlands',
    'es': 'Español',
    'fr': 'Français',
    'pt_pt': 'Português (pt)',
    'pt_br': 'Português (br)',
    'de': 'Deutsch',
    'it': 'Italiano',
    'sw': 'Swahili',
    'hu': 'Magyar',
    'el': 'Ελληνικά',
    'zh': "简体中文",
    'cs': 'Čeština',
    'bn': 'বাংলা',
    'hi': 'हिंदी'
}

LEVEL_DEFAULTS = collections.defaultdict(courses.NoSuchDefaults)
for lang in ALL_LANGUAGES.keys():
    LEVEL_DEFAULTS[lang] = courses.LevelDefaults(lang)

HEDY_COURSE = collections.defaultdict(courses.NoSuchCourse)
for lang in ALL_LANGUAGES.keys():
    HEDY_COURSE[lang] = courses.Course('hedy', lang, LEVEL_DEFAULTS[lang])

ONLINE_MASTERS_COURSE = courses.Course('online_masters', 'nl', LEVEL_DEFAULTS['nl'])

TRANSLATIONS = hedyweb.Translations()

DATABASE = database.Database()

def load_adventures_in_all_languages():
    adventures = {}
    for lang in ALL_LANGUAGES.keys ():
        adventures[lang] = load_yaml(f'coursedata/adventures/{lang}.yaml')
    return adventures


def load_adventure_for_language(lang):
    adventures = load_adventures_in_all_languages()
    if not lang in adventures or len (adventures [lang]) == 0:
        return adventures ['en']
    return adventures [lang]


def load_adventures_per_level(lang, level):

    loaded_programs = {}
    # If user is logged in, we iterate their programs that belong to the current level. Out of these, we keep the latest created program for both the level mode (no adventure) and for each of the adventures.
    if current_user (request) ['username']:
        user_programs = DATABASE.programs_for_user(current_user (request) ['username'])
        for program in user_programs:
            if program ['level'] != level:
                continue
            program_key = 'level' if not program.get ('adventure_name') else program ['adventure_name']
            if not program_key in loaded_programs:
                loaded_programs [program_key] = program
            elif loaded_programs [program_key] ['date'] < program ['date']:
                loaded_programs [program_key] = program

    all_adventures = []
    adventures = load_adventure_for_language(lang)['adventures']
    for short_name, adventure in adventures.items ():
        if not level in adventure['levels']:
            continue
        all_adventures.append({
            'short_name': short_name,
            'name': adventure['name'],
            'image': adventure.get('image', None),
            'default_save_name': adventure['default_save_name'],
            'text': adventure['levels'][level].get('story_text', 'No Story Text'),
            'start_code': adventure['levels'][level].get ('start_code', ''),
            'loaded_program': '' if not loaded_programs.get (short_name) else {
                'name': loaded_programs.get (short_name) ['name'],
                'code': loaded_programs.get (short_name) ['code']
             }
        })
    # We create a 'level' pseudo assignment to store the loaded program for level mode, if any.
    all_adventures.append({
        'short_name': 'level',
        'loaded_program': '' if not loaded_programs.get ('level') else {
            'name': loaded_programs.get ('level') ['name'],
            'code': loaded_programs.get ('level') ['code']
         }
    })
    return all_adventures

# Load main menu (do it once, can be cached)
with open(f'main/menu.json', 'r', encoding='utf-8') as f:
    main_menu_json = json.load(f)

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)-8s: %(message)s')


app = Flask(__name__, static_url_path='')
# Ignore trailing slashes in URLs
app.url_map.strict_slashes = False

cdn.Cdn(app, os.getenv('CDN_PREFIX'), os.getenv('HEROKU_SLUG_COMMIT', 'dev'))

# Set session id if not already set. This must be done as one of the first things,
# so the function should be defined high up.
@app.before_request
def set_session_cookie():
    session_id()

if os.getenv('IS_PRODUCTION'):
    @app.before_request
    def reject_e2e_requests():
        if utils.is_testing_request (request):
            return 'No E2E tests are allowed in production', 400

@app.before_request
def before_request_proxy_testing():
    if utils.is_testing_request (request):
        if os.getenv ('IS_TEST_ENV'):
            session ['test_session'] = 'test'

# HTTP -> HTTPS redirect
# https://stackoverflow.com/questions/32237379/python-flask-redirect-to-https-from-http/32238093
if os.getenv ('REDIRECT_HTTP_TO_HTTPS'):
    @app.before_request
    def before_request_https():
        if request.url.startswith('http://'):
            url = request.url.replace('http://', 'https://', 1)
            # We use a 302 in case we need to revert the redirect.
            return redirect(url, code=302)

# Unique random key for sessions.
# For settings with multiple workers, an environment variable is required, otherwise cookies will be constantly removed and re-set by different workers.
if utils.is_production():
    if not os.getenv ('SECRET_KEY'):
        raise RuntimeError('The SECRET KEY must be provided for non-dev environments.')

    app.config['SECRET_KEY'] = os.getenv ('SECRET_KEY')

else:
    app.config['SECRET_KEY'] = os.getenv ('SECRET_KEY', uuid.uuid4().hex)

if utils.is_heroku():
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
    )

# Set security attributes for cookies in a central place - but not when running locally, so that session cookies work well without HTTPS

Compress(app)
Commonmark(app)
parse_logger = jsonbin.MultiParseLogger(
    jsonbin.JsonBinLogger.from_env_vars(),
    jsonbin.S3ParseLogger.from_env_vars())
querylog.LOG_QUEUE.set_transmitter(aws_helpers.s3_querylog_transmitter_from_env())

# Check that requested language is supported, otherwise return 404
@app.before_request
def check_language():
    if requested_lang() not in ALL_LANGUAGES.keys ():
        return "Language " + requested_lang () + " not supported", 404

if utils.is_heroku() and not os.getenv('HEROKU_RELEASE_CREATED_AT'):
    logging.warning('Cannot determine release; enable Dyno metadata by running "heroku labs:enable runtime-dyno-metadata -a <APP_NAME>"')


@app.before_request
def before_request_begin_logging():
    querylog.begin_global_log_record(path=request.path, method=request.method)

@app.after_request
def after_request_log_status(response):
    querylog.log_value(http_code=response.status_code)
    return response

@app.after_request
def set_security_headers(response):
    security_headers = {
        'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
        'X-Frame-Options': 'DENY',
        'X-XSS-Protection': '1; mode=block',
    }
    response.headers.update(security_headers)
    return response

@app.teardown_request
def teardown_request_finish_logging(exc):
    querylog.finish_global_log_record(exc)

# If present, PROXY_TO_TEST_HOST should be the 'http[s]://hostname[:port]' of the target environment
if os.getenv ('PROXY_TO_TEST_HOST') and not os.getenv ('IS_TEST_ENV'):
    ab_proxying.ABProxying(app, os.getenv ('PROXY_TO_TEST_HOST'), app.config['SECRET_KEY'])

@app.route('/session_test', methods=['GET'])
def echo_session_vars_test():
    if not utils.is_testing_request (request):
        return 'This endpoint is only meant for E2E tests', 400
    return jsonify({'session': dict(session)})

@app.route('/session_main', methods=['GET'])
def echo_session_vars_main():
    if not utils.is_testing_request (request):
        return 'This endpoint is only meant for E2E tests', 400
    return jsonify({'session': dict(session), 'proxy_enabled': bool (os.getenv ('PROXY_TO_TEST_HOST'))})

@app.route('/parse', methods=['POST'])
def parse():
    body = request.json
    if not body:
        return "body must be an object", 400
    if 'code' not in body:
        return "body.code must be a string", 400
    if 'level' not in body:
        return "body.level must be a string", 400
    if 'sublevel' in body and not isinstance(body ['sublevel'], int):
        return "If present, body.sublevel must be an integer", 400
    if 'adventure_name' in body and not isinstance(body ['adventure_name'], str):
        return "if present, body.adventure_name must be a string", 400

    code = body ['code']
    level = int(body ['level'])
    sublevel = body.get ('sublevel') or 0

    # Language should come principally from the request body,
    # but we'll fall back to browser default if it's missing for whatever
    # reason.
    lang = body.get('lang', requested_lang())

    response = {}
    username = current_user(request) ['username'] or None

    querylog.log_value(level=level, lang=lang, session_id=session_id(), username=username)

    # Check if user sent code
    if not code:
        response["Error"] = "no code found, please send code."
    # is so, parse
    else:
        try:
            hedy_errors = TRANSLATIONS.get_translations(lang, 'HedyErrorMessages')
            with querylog.log_time('transpile'):
                transpile_result = hedy.transpile(code, level, sublevel)
                python_code = transpile_result.code
                has_turtle = transpile_result.has_turtle

            response['has_turtle'] = has_turtle
            if has_turtle:
                response["Code"] = "# coding=utf8\nimport random\nimport turtle\nt = turtle.Turtle()\nt.forward(0)\n" + python_code
            else:
                response["Code"] = "# coding=utf8\nimport random\n" + python_code
        except hedy.HedyException as E:
            traceback.print_exc()
            # some 'errors' can be fixed, for these we throw an exception, but also
            # return fixed code, so it can be ran
            if E.args[0] == "Invalid Space":
                error_template = hedy_errors[E.error_code]
                response["Code"] = "# coding=utf8\n" + E.arguments['fixed_code']
                response["Warning"] = error_template.format(**E.arguments)
            elif E.args[0] == "Parse":
                error_template = hedy_errors[E.error_code]
                # Localize the names of characters. If we can't do that, just show the original
                # character.
                if 'character_found' in E.arguments.keys():
                    E.arguments['character_found'] = hedy_errors.get(E.arguments['character_found'], E.arguments['character_found'])
                elif 'keyword_found' in E.arguments.keys():
                    #if we find an invalid keyword, place it in the same location in the error message but without translating
                    E.arguments['character_found'] = E.arguments['keyword_found']

                response["Error"] = error_template.format(**E.arguments)
            elif E.args[0] == "Unquoted Text":
                error_template = hedy_errors[E.error_code]
                response["Error"] = error_template.format(**E.arguments)
            elif E.args[0] == "Has Blanks":
                error_template = hedy_errors[E.error_code]
                response["Error"] = error_template.format(**E.arguments)
            else:
                error_template = hedy_errors[E.error_code]
                response["Error"] = error_template.format(**E.arguments)
        except Exception as E:
            traceback.print_exc()
            print(f"error transpiling {code}")
            response["Error"] = str(E)
    querylog.log_value(server_error=response.get('Error'))
    parse_logger.log ({
        'session': session_id(),
        'date': str(datetime.datetime.now()),
        'level': level,
        'lang': lang,
        'code': code,
        'server_error': response.get('Error'),
        'version': version(),
        'username': username,
        'is_test': 1 if os.getenv ('IS_TEST_ENV') else None,
        'adventure_name': body.get('adventure_name', None)
    })

    return jsonify(response)

@app.route('/report_error', methods=['POST'])
def report_error():
    post_body = request.json

    parse_logger.log ({
        'session': session_id(),
        'date': str(datetime.datetime.now()),
        'level': post_body.get('level'),
        'code': post_body.get('code'),
        'client_error': post_body.get('client_error'),
        'version': version(),
        'username': current_user(request) ['username'] or None,
        'is_test': 1 if os.getenv ('IS_TEST_ENV') else None
    })

    return 'logged'

@app.route('/client_exception', methods=['POST'])
def report_client_exception():
    post_body = request.json

    querylog.log_value(
        session=session_id(),
        date=str(datetime.datetime.now()),
        client_error=post_body,
        version=version(),
        username=current_user(request) ['username'] or None,
        is_test=1 if os.getenv ('IS_TEST_ENV') else None
    )

    # Return a 500 so the HTTP status codes will stand out in our monitoring/logging
    return 'logged', 500

@app.route('/version', methods=['GET'])
def version_page():
    """
    Generate a page with some diagnostic information and a useful GitHub URL on upcoming changes.

    This is an admin-only page, it does not need to be linked.
    (Also does not have any sensitive information so it's fine to be unauthenticated).
    """
    app_name = os.getenv('HEROKU_APP_NAME')

    vrz = os.getenv('HEROKU_RELEASE_CREATED_AT')
    the_date = datetime.date.fromisoformat(vrz[:10]) if vrz else datetime.date.today()

    commit = os.getenv('HEROKU_SLUG_COMMIT', '????')[0:6]

    return render_template('version-page.html',
        app_name=app_name,
        heroku_release_time=the_date,
        commit=commit)


def programs_page (request):
    username = current_user(request) ['username']
    if not username:
        # redirect users to /login if they are not logged in
        url = request.url.replace('/programs', '/login')
        return redirect(url, code=302)

    from_user = request.args.get('user') or None
    if from_user and not is_admin (request):
        if not is_teacher (request):
            return "unauthorized", 403
        students = DATABASE.get_teacher_students (username)
        if from_user not in students:
            return "unauthorized", 403

    texts=TRANSLATIONS.get_translations (requested_lang (), 'Programs')
    ui=TRANSLATIONS.get_translations (requested_lang (), 'ui')
    adventures = load_adventure_for_language(requested_lang ())['adventures']

    result = DATABASE.programs_for_user(from_user or username)
    programs = []
    now = timems ()
    for item in result:
        program_age = now - item ['date']
        if program_age < 1000 * 60 * 60:
            measure = texts ['minutes']
            date = round (program_age / (1000 * 60))
        elif program_age < 1000 * 60 * 60 * 24:
            measure = texts ['hours']
            date = round (program_age / (1000 * 60 * 60))
        else:
            measure = texts ['days']
            date = round (program_age / (1000 * 60 * 60 * 24))

        programs.append ({'id': item ['id'], 'code': item ['code'], 'date': texts ['ago-1'] + ' ' + str (date) + ' ' + measure + ' ' + texts ['ago-2'], 'level': item ['level'], 'name': item ['name'], 'adventure_name': item.get ('adventure_name'), 'public': item.get ('public')})

    return render_template('programs.html', lang=requested_lang(), menu=render_main_menu('programs'), texts=texts, ui=ui, auth=TRANSLATIONS.get_translations (requested_lang (), 'Auth'), programs=programs, username=username, is_teacher=is_teacher (request), current_page='programs', from_user=from_user, adventures=adventures)

@app.route('/quiz/start/<level>', methods=['GET'])
def get_quiz_start(level):
    if not config['quiz-enabled'] and g.lang != 'nl':
        return 'Hedy quiz disabled!', 404
    else:
        g.lang = lang = requested_lang()
        g.prefix = '/hedy'

         #Sets the values of total_score and correct on the beginning of the quiz at 0
        session['total_score'] = 0
        session['correct_answer'] = 0
        return render_template('startquiz.html', level=level, next_assignment=1, menu=render_main_menu('adventures'),
                               lang=lang,
                               username=current_user(request)['username'], is_teacher=is_teacher (request),
                               auth=TRANSLATIONS.get_translations (requested_lang(), 'Auth'))


# Quiz mode
# Fill in the filename as source
@app.route('/quiz/quiz_questions/<level_source>/<question_nr>/<attempt>', methods=['GET'])
def get_quiz(level_source, question_nr, attempt):
    if not config['quiz-enabled'] and g.lang != 'nl':
        return 'Hedy quiz disabled!', 404
    else:
        # Reading the yaml file
        if os.path.isfile(f'coursedata/quiz/quiz_questions_lvl{level_source}.yaml'):
            quiz_data = load_yaml(f'coursedata/quiz/quiz_questions_lvl{level_source}.yaml')
        else:
            return 'No quiz yaml file found for this level', 404

        # set globals
        g.lang = lang = requested_lang()
        g.prefix = '/hedy'

        # Loop through the questions and check that the loop doesn't reach out of bounds
        q_nr = int(question_nr)
        if q_nr <= len(quiz_data['questions']):
            question = quiz_data['questions'][q_nr - 1].get(q_nr)

            # Convert the indices to the corresponding characters
            char_array = []
            for i in range(len(question['mp_choice_options'])):
                char_array.append(chr(ord('@') + (i + 1)))
            return render_template('quiz_question.html', quiz=quiz_data, level_source=level_source,
                                   questions=quiz_data['questions'],
                                   question=quiz_data['questions'][q_nr - 1].get(q_nr), question_nr=q_nr,
                                   correct=session.get('correct_answer'),
                                   attempt = attempt,
                                   char_array=char_array,
                                   menu=render_main_menu('adventures'), lang=lang,
                                   username=current_user(request)['username'],
                                   is_teacher=is_teacher(request),
                                   auth=TRANSLATIONS.get_translations (requested_lang(), 'Auth'))
        else:
            return render_template('endquiz.html', correct=session.get('correct_answer'),
                                   total_score=session.get('total_score'),
                                   menu=render_main_menu('adventures'), lang=lang,
                                   quiz=quiz_data, level=int(level_source) + 1, questions=quiz_data['questions'],
                                   next_assignment=1, username=current_user(request)['username'],
                                   is_teacher=is_teacher(request),
                                   auth=TRANSLATIONS.get_translations (requested_lang(), 'Auth'))

@app.route('/submit_answer/<level_source>/<question_nr>/<attempt>', methods=["POST"])
def submit_answer(level_source, question_nr, attempt):
    if not config['quiz-enabled'] and g.lang != 'nl':
        return 'Hedy quiz disabled!', 404
    else:
        # Get the chosen option from the request form with radio buttons
        option = request.form["radio_option"]

        # Reading yaml file
        if os.path.isfile(f'coursedata/quiz/quiz_questions_lvl{level_source}.yaml'):
            quiz_data = load_yaml(f'coursedata/quiz/quiz_questions_lvl{level_source}.yaml')
        else:
            return 'No quiz yaml file found for this level', 404

        # Convert question_nr to an integer
        q_nr = int(question_nr)

        session['quiz-attempt'] = int(attempt)

        # Convert the corresponding chosen option to the index of an option
        question = quiz_data['questions'][q_nr - 1].get(q_nr)
        index_option = ord(option.split("-")[1]) - 65

        # If the correct answer is chosen, update the total score and the number of correct answered questions
        if question['correct_answer'] in option:
            if session.get('total_score'):
                session['total_score'] = session.get('total_score') +(config['quiz-max-attempts'] -  session.get('quiz-attempt')  )* 0.5 * question['question_score']
            else:
                session['total_score'] = (config['quiz-max-attempts'] - session.get('quiz-attempt')  )* 0.5 * question['question_score']
            if session.get('correct_answer'):
                session['correct_answer'] = session.get('correct_answer') + 1
            else:
                session['correct_answer'] = 1
        # Loop through the questions and check that the loop doesn't reach out of bounds
        q_nr = int(question_nr)
        if q_nr <= len(quiz_data['questions']) :
            if question['correct_answer'] in option:
                return render_template('feedback.html', quiz=quiz_data, question=question,
                                       questions=quiz_data['questions'],
                                       level_source=level_source,
                                       question_nr=q_nr,
                                       correct=session.get('correct_answer'),
                                       option=option,
                                       index_option=index_option,
                                       menu=render_main_menu('adventures'), lang=lang,
                                       username=current_user(request)['username'],
                                       auth=TRANSLATIONS.data[requested_lang()]['Auth'])
            elif session.get('quiz-attempt')  < config['quiz-max-attempts']:
                question = quiz_data['questions'][q_nr - 1].get(q_nr)
                # Convert the indices to the corresponding characters
                char_array = []
                for i in range(len(question['mp_choice_options'])):
                    char_array.append(chr(ord('@') + (i + 1)))
                return render_template('quiz_question.html', quiz=quiz_data, level_source=level_source,
                                       questions=quiz_data['questions'],
                                       question=quiz_data['questions'][q_nr - 1].get(q_nr), question_nr=q_nr,
                                       correct=session.get('correct_answer'),
                                       attempt= session.get('quiz-attempt') ,
                                       questionFalse='false',
                                       char_array=char_array,
                                       menu=render_main_menu('adventures'), lang=lang,
                                       username=current_user(request)['username'],
                                       auth=TRANSLATIONS.data[requested_lang()]['Auth'])
            elif session.get('quiz-attempt')  > config['quiz-max-attempts']:
                return render_template('feedback.html', quiz=quiz_data, question=question,
                                       questions=quiz_data['questions'],
                                       level_source=level_source,
                                       question_nr=q_nr,
                                       correct=session.get('correct_answer'),
                                       questionFalse = 'false',
                                       option=option,
                                       index_option=index_option,
                                       menu=render_main_menu('adventures'), lang=lang,
                                       username=current_user(request)['username'],
                                       auth=TRANSLATIONS.data[requested_lang()]['Auth'])
        else:  # show a different page for after the last question
            return 'No end quiz page!', 404

# Adventure mode
@app.route('/hedy/adventures', methods=['GET'])
def adventures_list():
    return render_template('adventures.html', lang=lang, adventures=load_adventure_for_language (requested_lang ()), menu=render_main_menu('adventures'), username=current_user(request) ['username'], is_teacher=is_teacher(request), auth=TRANSLATIONS.get_translations (requested_lang (), 'Auth'))

@app.route('/hedy/adventures/<adventure_name>', methods=['GET'], defaults={'level': 1})
@app.route('/hedy/adventures/<adventure_name>/<level>', methods=['GET'])
def adventure_page(adventure_name, level):

    user = current_user (request)
    level = int (level)
    adventures = load_adventure_for_language (requested_lang ())

    # If requested adventure does not exist, return 404
    if not adventure_name in adventures ['adventures']:
        return 'No such Hedy adventure!', 404

    adventure = adventures ['adventures'] [adventure_name]

    # If no level is specified (this will happen if the last element of the path (minus the query parameter) is the same as the adventure_name)
    if re.sub (r'\?.+', '', request.url.split ('/') [len (request.url.split ('/')) - 1]) == adventure_name:
        # If user is logged in, check if they have a program for this adventure
        # If there are many, note the highest level for which there is a saved program
        desired_level = 0
        if user ['username']:
            existing_programs = DATABASE.programs_for_user(user ['username'])
            for program in existing_programs:
                if 'adventure_name' in program and program ['adventure_name'] == adventure_name and program ['level'] > desired_level:
                    desired_level = program ['level']
            # If the user has a saved program for this adventure, redirect them to the level with the highest adventure
            if desired_level != 0:
                return redirect(request.url.replace ('/' + adventure_name, '/' + adventure_name + '/' + str (desired_level)), code=302)
        # If user is not logged in, or has no saved programs for this adventure, default to the lowest level available for the adventure
        if desired_level == 0:
            for key in adventure ['levels'].keys ():
                if isinstance(key, int) and (desired_level == 0 or desired_level > key):
                    desired_level = key
        level = desired_level

    # If requested level is not in adventure, return 404
    if not level in adventure ['levels']:
        abort(404)

    adventures = load_adventures_per_level(requested_lang(), level)
    g.prefix = '/hedy'
    return hedyweb.render_code_editor_with_tabs(
        request=request,
        course=HEDY_COURSE[requested_lang()],
        level_number=level,
        menu=render_main_menu('hedy'),
        translations=TRANSLATIONS,
        version=version(),
        adventures=adventures,
        # The relevant loaded program will be available to client-side js and it will be loaded by js.
        loaded_program='',
        adventure_name=adventure_name)

# routing to index.html
@app.route('/ontrack', methods=['GET'], defaults={'level': '1', 'step': 1})
@app.route('/hedy', methods=['GET'], defaults={'level': '1', 'step': 1})
@app.route('/hedy/<level>', methods=['GET'], defaults={'step': 1})
@app.route('/hedy/<level>/<step>', methods=['GET'])
def index(level, step):
    # Sublevel requested
    if re.match ('\d+-\d+', level):
        pass
        # If level has a dash, we keep it as a string
    # Normal level requested
    elif re.match ('\d', level):
        try:
            g.level = level = int(level)
        except:
            return 'No such Hedy level!', 404
    else:
        return 'No such Hedy level!', 404

    g.lang = requested_lang()
    g.prefix = '/hedy'

    loaded_program = ''
    adventure_name = ''

    # If step is a string that has more than two characters, it must be an id of a program
    if step and isinstance(step, str) and len (step) > 2:
        result = DATABASE.program_by_id(step)
        if not result:
            return 'No such program', 404
        # If the program is not public, allow only the owner of the program, the admin user and the teacher users to access the program
        user = current_user (request)
        public_program = 'public' in result and result ['public']
        if not public_program and user ['username'] != result ['username'] and not is_admin (request) and not is_teacher (request):
            return 'No such program!', 404
        loaded_program = {'code': result ['code'], 'name': result ['name'], 'adventure_name': result.get ('adventure_name')}
        if 'adventure_name' in result:
            adventure_name = result ['adventure_name']

    adventures = load_adventures_per_level(g.lang, level)

    return hedyweb.render_code_editor_with_tabs(
        request=request,
        course=HEDY_COURSE[g.lang],
        level_number=level,
        menu=render_main_menu('hedy'),
        translations=TRANSLATIONS,
        version=version(),
        adventures=adventures,
        loaded_program=loaded_program,
        adventure_name=adventure_name)

@app.route('/hedy/<id>/view', methods=['GET'])
def view_program(id):
    g.lang = requested_lang()
    g.prefix = '/hedy'

    result = DATABASE.program_by_id(id)
    if not result:
        return 'No such program', 404

    # Default to the language of the program's author (but still respect)
    # the switch if given.
    lang = request.args.get("lang")
    if not lang:
        lang = result['lang']

    arguments_dict = {}
    arguments_dict['program_id'] = id
    arguments_dict['page_title'] = f'{result["name"]} – Hedy'
    arguments_dict['level'] = result['level']  # Necessary for running
    arguments_dict['loaded_program'] = result
    arguments_dict['editor_readonly'] = True
    arguments_dict['show_edit_button'] = True

    # Everything below this line has nothing to do with this page and it's silly
    # that every page needs to put in so much effort to re-set it
    arguments_dict['lang'] = lang
    arguments_dict['menu'] = render_main_menu('view')
    arguments_dict['auth'] = TRANSLATIONS.get_translations(lang, 'Auth')
    arguments_dict['username'] = current_user(request) ['username'] or None
    arguments_dict['is_teacher'] = is_teacher(request)
    arguments_dict.update(**TRANSLATIONS.get_translations(lang, 'ui'))

    return render_template("view-program-page.html", **arguments_dict)


@app.route('/onlinemasters', methods=['GET'], defaults={'level': 1, 'step': 1})
@app.route('/onlinemasters/<int:level>', methods=['GET'], defaults={'step': 1})
@app.route('/onlinemasters/<int:level>/<int:step>', methods=['GET'])
def onlinemasters(level, step):
    g.level = level = int(level)
    g.lang = lang = requested_lang()
    g.prefix = '/onlinemasters'

    adventures = load_adventures_per_level(g.lang, level)

    return hedyweb.render_code_editor_with_tabs(
        request=request,
        course=ONLINE_MASTERS_COURSE,
        level_number=level,
        translations=TRANSLATIONS,
        version=version(),
        menu=None,
        adventures=adventures,
        loaded_program='',
        adventure_name='')

@app.route('/space_eu', methods=['GET'], defaults={'level': 1, 'step': 1})
@app.route('/space_eu/<int:level>', methods=['GET'], defaults={'step': 1})
@app.route('/space_eu/<int:level>/<int:step>', methods=['GET'])
def space_eu(level, step):
    g.level = level = int(level)
    g.lang = requested_lang()
    g.prefix = '/space_eu'

    adventures = load_adventures_per_level(g.lang, level)

    return hedyweb.render_code_editor_with_tabs(
        request=request,
        course=HEDY_COURSE[g.lang],
        level_number=level,
        translations=TRANSLATIONS,
        version=version(),
        menu=None,
        adventures=adventures,
        loaded_program='',
        adventure_name='')



@app.route('/client_messages.js', methods=['GET'])
def client_messages():
    error_messages = TRANSLATIONS.get_translations(requested_lang(), "ClientErrorMessages")
    ui_messages = TRANSLATIONS.get_translations(requested_lang(), "ui")
    auth_messages = TRANSLATIONS.get_translations(requested_lang(), "Auth")

    response = make_response(render_template("client_messages.js",
        error_messages=json.dumps(error_messages),
        ui_messages=json.dumps(ui_messages),
        auth_messages=json.dumps(auth_messages)))

    if not is_debug_mode():
        # Cache for longer when not devving
        response.cache_control.max_age = 60 * 60  # Seconds

    return response

@app.errorhandler(500)
def internal_error(exception):
    import traceback
    print(traceback.format_exc())
    return "<h1>500 Internal Server Error</h1>", 500

@app.route('/index.html')
@app.route('/')
def default_landing_page():
    return main_page('start')

@app.route('/<page>')
def main_page(page):
    if page == 'favicon.ico':
        abort(404)

    lang = requested_lang()
    effective_lang = lang

    if page in ['signup', 'login', 'my-profile', 'recover', 'reset', 'admin']:
        return auth_templates(page, lang, render_main_menu(page), request)

    if page == 'programs':
        return programs_page(request)

    # Default to English if requested language is not available
    if not path.isfile(f'main/{page}-{effective_lang}.md'):
        effective_lang = 'en'

    try:
        with open(f'main/{page}-{effective_lang}.md', 'r', encoding='utf-8') as f:
            contents = f.read()
    except IOError:
        abort(404)

    front_matter, markdown = split_markdown_front_matter(contents)

    menu = render_main_menu(page)
    if page == 'for-teachers':
        teacher_classes = [] if not current_user (request) ['username'] else DATABASE.get_teacher_classes (current_user (request) ['username'], True)
        return render_template('for-teachers.html', sections=split_teacher_docs (contents), lang=lang, menu=menu, username=current_user(request) ['username'], is_teacher=is_teacher(request), auth=TRANSLATIONS.get_translations (lang, 'Auth'), teacher_classes=teacher_classes, **front_matter)

    return render_template('main-page.html', mkd=markdown, lang=lang, menu=menu, username=current_user(request) ['username'], is_teacher=is_teacher(request), auth=TRANSLATIONS.get_translations (lang, 'Auth'), **front_matter)


def session_id():
    """Returns or sets the current session ID."""
    if 'session_id' not in session:
        if os.getenv ('IS_TEST_ENV') and 'X-session_id' in request.headers:
            session['session_id'] = request.headers ['X-session_id']
        else:
            session['session_id'] = uuid.uuid4().hex
    return session['session_id']

def requested_lang():
    """Return the user's requested language code.

    If not in the request parameters, use the browser's accept-languages
    header to do language negotiation.
    """
    lang = request.args.get("lang")
    if lang: return lang

    return request.accept_languages.best_match(ALL_LANGUAGES.keys(), 'en')

@app.template_global()
def current_language():
    return make_lang_obj(requested_lang())

@app.template_global()
def hedy_link(level_nr, assignment_nr, subpage=None, lang=None):
    """Make a link to a Hedy page."""
    parts = [g.prefix]
    parts.append('/' + str(level_nr))
    if str(assignment_nr) != '1' or subpage:
        parts.append('/' + str(assignment_nr if assignment_nr else '1'))
    if subpage and subpage != 'code':
        parts.append('/' + subpage)
    parts.append('?')
    parts.append('lang=' + (lang if lang else requested_lang()))
    return ''.join(parts)

@app.template_global()
def other_languages():
    cl = requested_lang()
    return [make_lang_obj(l) for l in ALL_LANGUAGES.keys() if l != cl]

@app.template_global()
def localize_link(url):
    lang = requested_lang()
    if not lang:
        return url
    if '?' in url:
        return url + '&lang=' + lang
    else:
        return url + '?lang=' + lang

def make_lang_obj(lang):
    """Make a language object for a given language."""
    return {
        'sym': ALL_LANGUAGES[lang],
        'lang': lang
    }


@app.template_global()
def modify_query(**new_values):
    args = request.args.copy()

    for key, value in new_values.items():
        args[key] = value

    return '{}?{}'.format(request.path, url_encode(args))


def no_none_sense(d):
    """Remove all None values from a dict."""
    return {k: v for k, v in d.items() if v is not None}


def split_markdown_front_matter(md):
    parts = re.split('^---', md, 1, re.M)
    if len(parts) == 1:
        return {}, md
    # safe_load returns 'None' if the string is empty
    front_matter = yaml.safe_load(parts[0]) or {}
    if not isinstance(front_matter, dict):
      # There was some kind of parsing error
      return {}, md

    return front_matter, parts[1]

def split_teacher_docs (contents):
    tags = utils.markdown_to_html_tags (contents)
    sections = []
    for tag in tags:
        # Sections are divided by h2 tags
        if re.match ('^<h2>', str (tag)):
            tag = tag.contents [0]
            # We strip `page_title: ` from the first title
            if len (sections) == 0:
                tag = tag.replace ('page_title: ', '')
            sections.append ({'title': tag, 'content': ''})
        else:
            sections [-1] ['content'] += str (tag)

    return sections

def render_main_menu(current_page):
    """Render a list of (caption, href, selected, color) from the main menu."""
    return [dict(
        caption=item.get(requested_lang(), item.get('en', '???')),
        href='/' + item['_'],
        selected=(current_page == item['_']),
        accent_color=item.get('accent_color', 'white'),
        short_name=item['_']
    ) for item in main_menu_json['nav']]

# *** PROGRAMS ***

@app.route('/programs_list', methods=['GET'])
@requires_login
def list_programs (user):
    return {'programs': DATABASE.programs_for_user(user['username'])}

# Not very restful to use a GET to delete something, but indeed convenient; we can do it with a single link and avoiding AJAX.
@app.route('/programs/delete/<program_id>', methods=['GET'])
@requires_login
def delete_program (user, program_id):
    result = DATABASE.program_by_id(program_id)
    if not result or result ['username'] != user ['username']:
        return "", 404
    DATABASE.delete_program_by_id(program_id)
    DATABASE.increase_user_program_count(user['username'], -1)
    return redirect ('/programs')

@app.route('/programs', methods=['POST'])
@requires_login
def save_program (user):

    body = request.json
    if not isinstance (body, dict):
        return 'body must be an object', 400
    if not isinstance (body.get('code'), str):
        return 'code must be a string', 400
    if not isinstance (body.get('name'), str):
        return 'name must be a string', 400
    if not isinstance (body.get('level'), int):
        return 'level must be an integer', 400
    if 'adventure_name' in body:
        if not isinstance (body.get('adventure_name'), str):
            return 'if present, adventure_name must be a string', 400

    # We check if a program with a name `xyz` exists in the database for the username.
    # It'd be ideal to search by username & program name, but since DynamoDB doesn't allow searching for two indexes at the same time, this would require to create a special index to that effect, which is cumbersome.
    # For now, we bring all existing programs for the user and then search within them for repeated names.
    programs = DATABASE.programs_for_user(user ['username'])
    program_id = uuid.uuid4().hex
    overwrite = False
    for program in programs:
        if program ['name'] == body ['name']:
            overwrite = True
            program_id = program ['id']
            break

    stored_program = {
        'id': program_id,
        'session': session_id(),
        'date': timems (),
        'lang': requested_lang(),
        'version': version(),
        'level': body ['level'],
        'code': body ['code'],
        'name': body ['name'],
        'username': user ['username']
    }

    if 'adventure_name' in body:
        stored_program ['adventure_name'] = body ['adventure_name']

    DATABASE.store_program(stored_program)
    if not overwrite:
        DATABASE.increase_user_program_count(user ['username'])

    return jsonify({'name': body ['name'], 'id': program_id})

@app.route('/programs/share', methods=['POST'])
@requires_login
def share_unshare_program(user):
    body = request.json
    if not isinstance (body, dict):
        return 'body must be an object', 400
    if not isinstance (body.get('id'), str):
        return 'id must be a string', 400
    if not isinstance (body.get('public'), bool):
        return 'public must be a string', 400

    result = DATABASE.program_by_id(body['id'])
    if not result or result ['username'] != user ['username']:
        return 'No such program!', 404

    DATABASE.set_program_public_by_id(body ['id'], bool(body ['public']))
    return jsonify({'id': body ['id']})

@app.route('/translate/<source>/<target>')
def translate_fromto(source, target):
    # FIXME: right now loading source file on demand. We might need to cache this...
    source_adventures = load_yaml(f'coursedata/adventures/{source}.yaml')
    source_levels = load_yaml(f'coursedata/level-defaults/{source}.yaml')
    source_texts = load_yaml(f'coursedata/texts/{source}.yaml')

    target_adventures = load_yaml(f'coursedata/adventures/{target}.yaml')
    target_levels = load_yaml(f'coursedata/level-defaults/{target}.yaml')
    target_texts = load_yaml(f'coursedata/texts/{target}.yaml')

    files = []

    files.append(translating.TranslatableFile(
      'Levels',
      f'level-defaults/{target}.yaml',
      translating.struct_to_sections(source_levels, target_levels)))

    files.append(translating.TranslatableFile(
      'Messages',
      f'texts/{target}.yaml',
      translating.struct_to_sections(source_texts, target_texts)))

    files.append(translating.TranslatableFile(
      'Adventures',
      f'adventures/{target}.yaml',
      translating.struct_to_sections(source_adventures, target_adventures)))

    return render_template('translate-fromto.html',
        source_lang=source,
        target_lang=target,
        files=files)

@app.route('/update_yaml', methods=['POST'])
def update_yaml():
    filename = path.join('coursedata', request.form['file'])
    # The file MUST point to something inside our 'coursedata' directory
    # (no exploiting bullshit here)
    filepath = path.abspath(filename)
    expected_path = path.abspath('coursedata')
    if not filepath.startswith(expected_path):
        raise RuntimeError('Are you trying to trick me?')

    data = load_yaml_rt(filepath)
    for key, value in request.form.items():
        if key.startswith('c:'):
            translating.apply_form_change(data, key[2:], translating.normalize_newlines(value))

    data = translating.normalize_yaml_blocks(data)

    return Response(dump_yaml_rt(data),
        mimetype='application/x-yaml',
        headers={'Content-disposition': 'attachment; filename=' + request.form['file'].replace('/', '-')})


# *** AUTH ***

from website import auth
auth.routes (app, DATABASE, requested_lang)

# *** TEACHER BACKEND

from website import teacher
teacher.routes (app, DATABASE, requested_lang)

# *** START SERVER ***

def on_server_start():
    """Called just before the server is started, both in developer mode and on Heroku.

    Use this to initialize objects, dependencies and connections.
    """
    pass


if __name__ == '__main__':
    # Start the server on a developer machine. Flask is initialized in DEBUG mode, so it
    # hot-reloads files. We also flip our own internal "debug mode" flag to True, so our
    # own file loading routines also hot-reload.
    utils.set_debug_mode(True)

    # If we are running in a Python debugger, don't use flasks reload mode. It creates
    # subprocesses which make debugging harder.
    is_in_debugger = sys.gettrace() is not None

    on_server_start()

    # Threaded option enables multiple instances for multiple user access support
    app.run(threaded=True, debug=not is_in_debugger, port=config ['port'], host="0.0.0.0")

    # See `Procfile` for how the server is started on Heroku.
