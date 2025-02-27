from datetime import timedelta
import json
import jwt
import redis

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from backend.backend import (Authorization, PasswordRecovery,
                             Registration, is_valid_email)
from config import (SECRET_KEY, SENTRY_DNS, SESSION_STATE_CODE,
                    SESSION_STATE_MAIL)
from database.FDataBase import select_by_email, select_by_user, update_is_active
from jwt_tools.jwt import create_jwt_token, decode_jwt_token
from models.models import (CodeConfirm, PasswordChange,
                           Recover, UserAuth, UserReg)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


async def get_current_user(token: str = Depends(oauth2_scheme)):
    """
    Проверка токена в Redis.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not redis_client.exists(token):
        raise credentials_exception

    try:
        payload = decode_jwt_token(token, SECRET_KEY)
        login = payload.get("login")
        if login is None:
            raise credentials_exception
    except jwt.JWTError:
        raise credentials_exception
    return {"login": login}


sentry_sdk.init(
    dsn=SENTRY_DNS,
    integrations=[
        FastApiIntegration(),
    ],
    traces_sample_rate=1.0,
)


# Роутеры, для формы регистрации
app_reg = APIRouter(prefix="/registration")
# Роутеры, для формы авторизации
app_auth = APIRouter(prefix="/authorization")
# Роутеры для логаута
app_logout = APIRouter(prefix="/logout")


# ПОдключение Redis
redis_client = redis.Redis(host='localhost', port=6379, db=0)


@app_reg.post("/")
async def registration(data: UserReg) -> JSONResponse:
    """
    Регистрация пользователя.

    Args:

        email: Почта.
        login: Логин.
        password: Пароль.
        password_two: Повтор пароля.

    Returns:

        JSONResponse: Результат регистрации.

        - 200: Успешное подтверждение, возвращает сообщение об успехе.
        - 422: Ошибка валидации, возвращает сообщение об ошибке.
        - Другие коды: Соответствующие сообщения об ошибках и коды статусов.

    Notes:

        1. Валидирует данные пользователя.
        2. Сохраняет временные данные в Redis
        3. Отправляет сгенерированный проверочный код на почту.
    """
    result = await Registration.register(data.email,
                                         data.login,
                                         data.password,
                                         data.password_two)
    if result['status_code'] == 200:
        data_redis = {
            "email": data.email,
            "login": data.login,
            "password": data.password,
        }
        redis_client.set(result['code'], json.dumps(data_redis))
        response = JSONResponse(content={"message": "Введите код с почты!"},
                                status_code=200)
    else:
        sentry_sdk.capture_message(result["message"])
        response = JSONResponse(content={"message": result["message"]},
                                status_code=result["status_code"])
    return response


@app_reg.post("/confirm")
async def confirm(data: CodeConfirm) -> JSONResponse:
    """
    Обработка формы ввода кода подтверждения регистрации.

    Args:

        code: Код из из почты.

    Returns:

        JSONResponse: Результат подтверждения кода.

        - 200: Успешное подтверждение, возвращает сообщение об успехе.
        - 422: Ошибка валидации, возвращает сообщение об ошибке.
        - 400: Ошибка подтверждения, возвращает соответствующее сообщение.

    Notes:

        1. Валидирует данные.
        2. По коду с почты достаёт данные из Redis.
        3. сохраняет пользователя в базу данных.
        * Пароль сохраняется в виде хэша.
        4. Очищает Redis от временных данных.
    """
    if not redis_client.exists(data.code):
        return JSONResponse(
            content={"message": "Введённый код не верный!"},
            status_code=400)
    user_data = redis_client.get(data.code)
    user_data = json.loads(user_data.decode('utf-8'))
    if isinstance(user_data, JSONResponse):
        return user_data
    email = user_data.get('email')
    login = user_data.get('login')
    password = user_data.get('password')

    result = await Registration.confirm_register(
        email, login, password)

    if result['status_code'] == 200:
        redis_client.delete(f"login:{data.code}")
        return JSONResponse(content={"message": result["message"]},
                            status_code=200)
    else:
        return JSONResponse(content={"message": result["message"]},
                            status_code=400)


@app_auth.post("/")
async def authorization(data: UserAuth) -> JSONResponse:
    """
    Обработчик логики авторизации.

    Args:

        login: Логин пользователя,
        password: Пароль пользователя,
        memorize_user: Булево значение(запомнить пользователя).

    Returns:

        JSONResponse: Результат авторизации.
        - 200: Успешная авторизация, возвращает ключ 'key' (login).
        - 422: Ошибка валидации, возвращает сообщение об ошибке.
        - Другие коды: Соответствующие сообщения об ошибках и коды статусов.

    Notes:

        1. Валидирует данные.
        2. На бэкенде проверяет наличие пользователя в бд,
        отправляет код на почту.
        3. Сохраняет временные данные в Redis.
    """
    result = await Authorization.authorization(data.login,
                                               data.password)
    if result['status_code'] == 200:
        data_redis = {
            "code": result['code'],
            "login": data.login,
            "remember_user": data.memorize_user
        }
        redis_client.set(result['code'], json.dumps(data_redis))
        response = JSONResponse(content={"key": result["login"]},
                                status_code=200)
    else:
        response = JSONResponse(content={"message": result["message"]},
                                status_code=result['status_code'])
    return response


@app_auth.post("/verification")
async def verification(data: CodeConfirm) -> JSONResponse:
    """
    Обработка формы ввода кода подтверждения авторизации.

    Args:

        code: Код из почты.

    Returns:

        JSONResponse: Результат подтверждения кода.
        - 200: Успешное подтверждение, возвращает сообщение об успехе.
        - 422: Ошибка валидации, возвращает сообщение об ошибке.
        - Другие коды: Соответствующие сообщения об ошибках и коды статусов.

    Notes:

        1. Валидирует данные.
        2. Проверяет код через Redis.
        3. Генерирует JWT токен, добавляет его в заголовок ответа.
        4. Очищает временные данные в Redis.
    """

    if not redis_client.exists(data.code):
        response = JSONResponse(
            content={"message": "Введённый код не верный!"},
            status_code=400)
    user_data = redis_client.get(data.code)
    user_data = json.loads(user_data.decode('utf-8'))
    if isinstance(user_data, JSONResponse):
        return user_data
    login = user_data.get('login')
    auth_code = user_data.get('code')
    token = create_jwt_token(login=login,
                             token_lifetime_hours=1,
                             secret_key=SECRET_KEY)

    await update_is_active(login, True)

    redis_client.setex(token, timedelta(hours=12), login)
    headers = {"Authorization": f"Bearer {token}"}
    response = JSONResponse(content={"message": "Вы авторизированны!"},
                            headers=headers,
                            status_code=200)
    redis_client.delete(f"login:{auth_code}")
    return response


@app_auth.post("/recover")
async def recover(data: Recover) -> JSONResponse:
    """
    Обработчик логики восстановления (изменения) пароля.

    Args:

        user: Логин или почта введённая пользователем.

    Returns:

        JSONResponse: Результат восстановления пароля.

        - 200: Успешное подтверждение, возвращает сообщение об успехе.
        - 422: Ошибка валидации, возвращает сообщение об ошибке.
        - Другие коды: Соответствующие сообщения об ошибках и коды статусов.

    Notes:

        1. Валидирует данные
        2. По введённыи данным находит пользователя и отправляет на почту
        сгенерированный код.
        3. Сохраняет в Redis временные данные (состояние, код и юзера).
    """
    result = await PasswordRecovery.recover_pass(data.user)
    data_redis = {
        'state': SESSION_STATE_MAIL,
        'user': data.user
        }
    if result['status_code'] == 200:
        redis_client.set(result['code'], json.dumps(data_redis))
        response = JSONResponse(
            content={"message": "Теперь введите код с почты...",
                     "user": str(data.user)},
            status_code=200)
    else:
        response = JSONResponse(content={"message": result["message"]},
                                status_code=result['status_code'])
    return response


@app_auth.post("/recover/reset_code")
async def reset_code(data: CodeConfirm) -> JSONResponse:
    """
    Подтверждение восстановления пароля кодом с почты.

    Args:

        code: Код из из почты.

    Returns:

        JSONResponse: Результат подтверждения кода.

        - 200: Успешное подтверждение, возвращает сообщение об успехе.
        - 422: Ошибка валидации, возвращает сообщение об ошибке.
        - 400: Ошибка состояния сессии, почта не указана.
    Notes:

        1. Валидирует данные
        2. По коду с почты, достаёт данные из Redis.
        3. Сверяет идентификатор сессии, с идентификатором из Redis.
        4. Сверяет код из почты, с кодом из хранилища Redis.
        5. Сохраняет идентификатор сессии и пользователя, во временное
        хранилище Redis, по логину/почте введённым в шаге /recover,
        этот параметр передаются в теле успешного(код 200) ответа от /recover.
        6. Очищает старые временные данные в Redis.

        * Сессия очищается через 6 минут, если код неверный. Время изменяется
        в переменных окружения.
    """
    if not redis_client.exists(data.code):
        response = JSONResponse(
            content={"message": "Введённый код не верный!"},
            status_code=400)
    user_data = redis_client.get(data.code)
    user_data = json.loads(user_data.decode('utf-8'))
    if isinstance(user_data, JSONResponse):
        return user_data
    state = user_data.get('state')
    user = user_data.get('user')

    if state == SESSION_STATE_MAIL:
        data_redis = {
            "state": SESSION_STATE_CODE,
            'user': user
            }
        redis_client.set(user, json.dumps(data_redis))
        response = JSONResponse(
            content={"message": "Можете менять пароль!"}, status_code=200)
        redis_client.delete(f"login:{data.code}")
        return response
    else:
        return JSONResponse(content={"message": "Вы не указали почту!"},
                            status_code=400)


@app_auth.post("/recover/reset_code/change_password")
async def change_password(data: PasswordChange) -> JSONResponse:
    """
    Изменение пароля после восстановления.

    Args:

        "user": Логин/почта введённая в первом шаге /recovery
        "password": Новый пароль.
        "password_two": Подтверждение нового пароля.

    Returns:

        JSONResponse: Результат изменения пароля.
        - 200: Успешное подтверждение, возвращает сообщение об успехе.
        - 422: Ошибка валидации, возвращает сообщение об ошибке.
        - 400: Ошибка состояния сессии, код не введен.

    Notes:

        1. Валидирует данные
        2. По логину/почте введённой в предыдущем шаге /recover,
        достаёт данные из Redis.
        3. Сверяет идентификатор сессии, с идентификатором из Redis.
        4. Меняет пароль в базе данных(сохраняя его в виде хэша).
        5. Удаляет временные данные из Redis.

        * Сессия очищается через 6 минут, если код неверный. Время изменяется
        в переменных окружения.
    """
    user_data = redis_client.get(data.user)
    user_data = json.loads(user_data.decode('utf-8'))
    if isinstance(user_data, JSONResponse):
        return user_data
    state = user_data.get('state')
    user = user_data.get('user')

    if state == SESSION_STATE_CODE:
        result = await PasswordRecovery.new_password(user, data.password,
                                                     data.password_two)
        if result['status_code'] == 200:
            response = JSONResponse(content={"message": result["message"]},
                                    status_code=result['status_code'])
            redis_client.delete(f"login:{user}")
        else:
            response = JSONResponse(content={"message": result["message"]},
                                    status_code=result['status_code'])
        return response
    else:
        return JSONResponse(content={"message": "Вы не ввели код!"},
                            status_code=400)


@app_logout.post("/")
async def logout(token: str = Depends(oauth2_scheme)) -> JSONResponse:
    """
    Обработчик выхода пользователя.

    Args:

        request (Request): HTTP запрос.
        data (Token): Токен пользователя для выхода.

    Returns:

        JSONResponse: Результат выхода пользователя.
        - 200: Успешное подтверждение, возвращает сообщение об успехе.
        - 422: Ошибка валидации, возвращает сообщение об ошибке.
        - 400: Ошибка состояния сессии, код не введен.

    Notes:

        1. Получает токен из заголовка Authorization.
        2. Проверяет его наличие в Redis.
        3. Удаляет токен из Redis, тем самым отменяя авторизацию пользователя.
    """
    if redis_client.exists(token):
        redis_client.delete(token)
        login = decode_jwt_token(token, SECRET_KEY)
        await update_is_active(login['login'], True)
        return JSONResponse(content={"message": "Успешный выход!"},
                            status_code=200)
    else:
        return JSONResponse(content={"message": "Токен не найден"},
                            status_code=400)
