import datetime
import os
import pprint

import uvicorn
from pydantic import BaseSettings
from aiocache import Cache
from tempfile import mkdtemp
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from werkzeug.exceptions import Forbidden
from pylti1p3.contrib.starlette import StarletteOIDCLogin, StarletteMessageLaunch, StarletteRequest, \
    StarletteCacheDataStorage

from pylti1p3.deep_link_resource import DeepLinkResource
from pylti1p3.grade import Grade
from pylti1p3.lineitem import LineItem
from pylti1p3.tool_config import ToolConfJsonFile
from pylti1p3.registration import Registration


cache = Cache()

routes = [
    Mount('/static', app=StaticFiles(directory='static'), name='static')
]

app = FastAPI(routes=routes)
templates = Jinja2Templates('templates')

class Settings(BaseSettings):
    pass

settings=Settings()


PAGE_TITLE = 'Game Example'


def get_lti_config_path():
    return os.path.join(app.root_path, '..', 'configs', 'game.json')


def get_launch_data_storage():
    return StarletteCacheDataStorage(cache)


def get_jwk_from_public_key(key_name):
    key_path = os.path.join(app.root_path, '..', 'configs', key_name)
    f = open(key_path, 'r')
    key_content = f.read()
    jwk = Registration.get_jwk(key_content)
    f.close()
    return jwk

@app.get('/login/')
@app.post('/login/')
def login(request: Request):
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    launch_data_storage = get_launch_data_storage()

    starlette_request = StarletteRequest(request)
    target_link_uri = starlette_request.get_param('target_link_uri')
    if not target_link_uri:
        raise Exception('Missing "target_link_uri" param')

    oidc_login = StarletteOIDCLogin(starlette_request, tool_conf, launch_data_storage=launch_data_storage)
    return oidc_login\
        .enable_check_cookies()\
        .redirect(target_link_uri)


@app.post('/launch/')
def launch(request: Request):
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    starlette_request = StarletteRequest(request)
    launch_data_storage = get_launch_data_storage()
    message_launch = StarletteMessageLaunch(starlette_request, tool_conf, launch_data_storage=launch_data_storage)
    message_launch_data = message_launch.get_launch_data()
    pprint.pprint(message_launch_data)

    difficulty = message_launch_data.get('https://purl.imsglobal.org/spec/lti/claim/custom', {}) \
        .get('difficulty', None)
    if not difficulty:
        difficulty = request.query_params.get('difficulty', 'normal')

    tpl_kwargs = {
        'request': request,
        'page_title': PAGE_TITLE,
        'is_deep_link_launch': message_launch.is_deep_link_launch(),
        'launch_data': message_launch.get_launch_data(),
        'launch_id': message_launch.get_launch_id(),
        'curr_user_name': message_launch_data.get('name', ''),
        'curr_diff': difficulty
    }
    return templates.TemplateResponse('game.html', tpl_kwargs)


@app.get('/.well-known/jwks.json')
def get_jwks():
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    return JSONResponse({'keys': tool_conf.get_jwks()})


@app.api_route('/configure/{launch_id}/{difficulty}/', methods=['GET','POST'])
def configure(request:Request, launch_id, difficulty):
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    starlette_request = StarletteRequest(request)
    launch_data_storage = get_launch_data_storage()
    message_launch = StarletteMessageLaunch.from_cache(launch_id, starlette_request, tool_conf,
                                                           launch_data_storage=launch_data_storage)

    if not message_launch.is_deep_link_launch():
        raise Forbidden('Must be a deep link!')

    launch_url = app.url_path_for('launch')

    resource = DeepLinkResource()
    resource.set_url(launch_url + '?difficulty=' + difficulty) \
        .set_custom_params({'difficulty': difficulty}) \
        .set_title('Breakout ' + difficulty + ' mode!')

    html = message_launch.get_deep_link().output_response_form([resource])
    return html


@app.post('/api/score/{launch_id}/{earned_score}/{time_spent}/')
def score(request:Request, launch_id, earned_score, time_spent):
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    starlette_request = StarletteRequest(request)
    launch_data_storage = get_launch_data_storage()
    message_launch = StarletteMessageLaunch.from_cache(launch_id, starlette_request, tool_conf,
                                                           launch_data_storage=launch_data_storage)

    resource_link_id = message_launch.get_launch_data() \
        .get('https://purl.imsglobal.org/spec/lti/claim/resource_link', {}).get('id')

    if not message_launch.has_ags():
        raise Forbidden("Don't have grades!")

    sub = message_launch.get_launch_data().get('sub')
    timestamp = datetime.datetime.utcnow().isoformat() + 'Z'
    earned_score = int(earned_score)
    time_spent = int(time_spent)

    grades = message_launch.get_ags()
    sc = Grade()
    sc.set_score_given(earned_score) \
        .set_score_maximum(100) \
        .set_timestamp(timestamp) \
        .set_activity_progress('Completed') \
        .set_grading_progress('FullyGraded') \
        .set_user_id(sub)

    sc_line_item = LineItem()
    sc_line_item.set_tag('score') \
        .set_score_maximum(100) \
        .set_label('Score')
    if resource_link_id:
        sc_line_item.set_resource_id(resource_link_id)

    grades.put_grade(sc, sc_line_item)

    tm = Grade()
    tm.set_score_given(time_spent) \
        .set_score_maximum(999) \
        .set_timestamp(timestamp) \
        .set_activity_progress('Completed') \
        .set_grading_progress('FullyGraded') \
        .set_user_id(sub)

    tm_line_item = LineItem()
    tm_line_item.set_tag('time') \
        .set_score_maximum(999) \
        .set_label('Time Taken')
    if resource_link_id:
        tm_line_item.set_resource_id(resource_link_id)

    result = grades.put_grade(tm, tm_line_item)

    return JSONResponse({'success': True, 'result': result.get('body')})


@app.api_route('/api/scoreboard/{launch_id}/', methods=['GET', 'POST'])
def scoreboard(request:Request,launch_id):
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    starlette_request = StarletteRequest(request)
    launch_data_storage = get_launch_data_storage()
    message_launch = StarletteMessageLaunch.from_cache(launch_id, starlette_request, tool_conf,
                                                           launch_data_storage=launch_data_storage)

    resource_link_id = message_launch.get_launch_data() \
        .get('https://purl.imsglobal.org/spec/lti/claim/resource_link', {}).get('id')

    if not message_launch.has_nrps():
        raise Forbidden("Don't have names and roles!")

    if not message_launch.has_ags():
        raise Forbidden("Don't have grades!")

    ags = message_launch.get_ags()

    score_line_item = LineItem()
    score_line_item.set_tag('score') \
        .set_score_maximum(100) \
        .set_label('Score')
    if resource_link_id:
        score_line_item.set_resource_id(resource_link_id)

    scores = ags.get_grades(score_line_item)

    time_line_item = LineItem()
    time_line_item.set_tag('time') \
        .set_score_maximum(999) \
        .set_label('Time Taken')
    if resource_link_id:
        time_line_item.set_resource_id(resource_link_id)

    times = ags.get_grades(time_line_item)

    members = message_launch.get_nrps().get_members()
    scoreboard_result = []

    for sc in scores:
        result = {'score': sc['resultScore']}
        for tm in times:
            if tm['userId'] == sc['userId']:
                result['time'] = tm['resultScore']
                break
        for member in members:
            if member['user_id'] == sc['userId']:
                result['name'] = member.get('name', 'Unknown')
                break
        scoreboard_result.append(result)

    return JSONResponse(scoreboard_result)


if __name__ == '__main__':
    uvicorn.run(app=app, host='0.0.0.0', port=9001,
                ssl_keyfile="../configs/localhost+2-key.pem", ssl_certfile="../configs/localhost+2.pem")
