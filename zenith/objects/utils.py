import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import hashlib
import app.state.services
import app.state.sessions
import bcrypt
import zenith.zconfig as zconfig
from app.constants.privileges import Privileges
from app.state import website as zglob
from cmyui.logging import Ansi, log
from quart import render_template, session

if TYPE_CHECKING:
    from PIL import Image

def determine_plural(number:int):
    if int(number) != 1:
        return 's'
    else:
        return ''

def time_ago(time1, time2, time_limit:int=0):
    """Calculate time ago between two dates"""
    time_diff = time1 - time2
    timeago = datetime.datetime(1,1,1) + time_diff
    time_ago = ""
    if timeago.year-1 != 0:
        time_ago += "{} Year{} ".format(timeago.year-1, determine_plural(timeago.year-1))
        time_limit = time_limit + 1
    if timeago.month-1 !=0:
        time_ago += "{} Month{} ".format(timeago.month-1, determine_plural(timeago.month-1))
        time_limit = time_limit + 1
    if timeago.day-1 !=0 and not time_limit == 2:
        time_ago += "{} Day{} ".format(timeago.day-1, determine_plural(timeago.day-1))
        time_limit = time_limit + 1
    if timeago.hour != 0 and not time_limit == 2:
        time_ago += "{} Hour{} ".format(timeago.hour, determine_plural(timeago.hour))
        time_limit = time_limit + 1
    if timeago.minute != 0 and not time_limit == 2:
        time_ago += "{} Minute{} ".format(timeago.minute, determine_plural(timeago.minute))
        time_limit = time_limit + 1
    if not time_limit == 2:
        time_ago += "{} Second{} ".format(timeago.second, determine_plural(timeago.second))
    return time_ago

async def flash(status: str, msg: str, template: str) -> str:
    """Flashes a message on a specified template."""
    return await render_template(f'{template}.html', tohome=False, msg=msg, status=status)

async def flash_tohome(status: str, msg: str):
    """Flashes a message on home"""
    return await render_template(f'home.html', tohome=True,  msg=msg, status=status)

async def validate_captcha(data: str) -> bool:
    """Verify `data` with hcaptcha's API."""
    url = f'https://hcaptcha.com/siteverify'

    data = {
        'secret': zconfig.hCaptcha_secret,
        'response': data
    }

    async with zglob.http.post(url, data=data) as resp:
        if not resp or resp.status != 200:
            if zglob.config.debug:
                log('Failed to verify captcha: request failed.', Ansi.LRED)
            return False

        res = await resp.json()

        return res['success']

BANNERS_PATH = Path.cwd() / 'zenith/.data/banners'
BACKGROUND_PATH = Path.cwd() / 'zenith/.data/backgrounds'
def has_profile_customizations(user_id: int = 0) -> dict[str, bool]:
    print(f"{BANNERS_PATH=} {BACKGROUND_PATH=}")
    # check for custom banner image file
    for ext in ('jpg', 'jpeg', 'png', 'gif'):
        path = BANNERS_PATH / f'{user_id}.{ext}'
        if has_custom_banner := path.exists():
            break

    # check for custom background image file
    for ext in ('jpg', 'jpeg', 'png', 'gif'):
        path = BACKGROUND_PATH / f'{user_id}.{ext}'
        if has_custom_background := path.exists():
            break

    return {
        'banner' : has_custom_banner,
        'background': has_custom_background
    }

def crop_image(image: 'Image') -> 'Image':
    width, height = image.size
    if width == height:
        return image

    offset = int(abs(height-width) / 2)

    if width > height:
        image = image.crop([offset, 0, width-offset, height])
    else:
        image = image.crop([0, offset, width, height-offset])

    return image

async def updateSession(session, id:int=None):
    if 'id' in session['user_data']:
        id = session['user_data']['id']
    elif id != None:
        id = id
    else:
        raise ValueError('Could not get id of a user.')

    user_info = await app.state.services.database.fetch_one(
        'SELECT id, name, email, priv, '
        'pw_bcrypt, silence_end '
        'FROM users '
        'WHERE id = :id', {"id": id}
    )
    user_info = dict(user_info)
    if (user_info['priv'] & Privileges.MODERATOR or
        user_info['priv'] & Privileges.ADMINISTRATOR or
        user_info['priv'] & Privileges.DEVELOPER):
        is_staff = True
    else:
        is_staff = False

    session['authenticated'] = True
    #session['player'] = app.state.sessions.players.from_cache_or_sql(name=user_info['name'])
    session['user_data'] = {
        'id': user_info['id'],
        'name': user_info['name'],
        'email': user_info['email'],
        'priv': int(user_info['priv']),
        'silence_end': user_info['silence_end'],
        'is_staff': is_staff,
    }

def get_safe_name(name: str) -> str:
    """Returns the safe version of a username."""
    # Safe name should meet few criterias.
    # - Whole name should be lower letters.
    # - Space must be replaced with _
    return name.lower().replace(' ', '_')

async def fetch_geoloc(ip: str) -> str:
    """Fetches the country code corresponding to an IP."""
    url = f'http://ip-api.com/line/{ip}'

    async with zglob.http.get(url) as resp:
        if not resp or resp.status != 200:
            if zconfig.debug:
                log('Failed to get geoloc data: request failed.', Ansi.LRED)
            return 'xx'
        status, *lines = (await resp.text()).split('\n')
        if status != 'success':
            if zconfig.debug:
                log(f'Failed to get geoloc data: {lines[0]}.', Ansi.LRED)
            return 'xx'
        return lines[1].lower()

async def validate_password(user_id:int, password_text:str):
    res = await app.state.services.database.fetch_val(
        "SELECT pw_bcrypt FROM users WHERE id=:uid",
        {"uid": user_id}
    )
    if not res:
        raise IndexError("User not found")
        # cache and other related password information

    bcrypt_cache = zglob.cache['bcrypt']
    pw_bcrypt = res.encode()
    pw_md5 = hashlib.md5(password_text.encode()).hexdigest().encode()

    # check credentials (password) against db
    # intentionally slow, will cache to speed up
    if pw_bcrypt in bcrypt_cache:
        if pw_md5 != bcrypt_cache[pw_bcrypt]: # ~0.1ms
            return False
    else: # ~200ms
        if not bcrypt.checkpw(pw_md5, pw_bcrypt):
            return False

        # login successful; cache password for next login
        bcrypt_cache[pw_bcrypt] = pw_md5
    return True