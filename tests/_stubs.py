import sys
import types


def install_test_stubs() -> None:
    # Telegram
    telegram_mod = sys.modules.get("telegram") or types.ModuleType("telegram")
    telegram_constants = sys.modules.get("telegram.constants") or types.ModuleType("telegram.constants")
    telegram_ext = sys.modules.get("telegram.ext") or types.ModuleType("telegram.ext")
    telegram_error = sys.modules.get("telegram.error") or types.ModuleType("telegram.error")

    if (
        hasattr(telegram_error, "BadRequest")
        and hasattr(telegram_error, "TelegramError")
        and hasattr(telegram_mod, "InlineKeyboardButton")
    ):
        sys.modules["telegram"] = telegram_mod
        sys.modules["telegram.constants"] = telegram_constants
        sys.modules["telegram.ext"] = telegram_ext
        sys.modules["telegram.error"] = telegram_error
    else:
        class _ParseMode:
            MARKDOWN_V2 = "MARKDOWN_V2"
            MARKDOWN = "MARKDOWN"

        class _ChatAction:
            TYPING = "typing"

        class _CallbackContext:
            user_data = {}

        class _Update:
            pass

        class _WebAppInfo:
            def __init__(self, url=""):
                self.url = url

        class _InlineKeyboardButton:
            def __init__(self, text, callback_data=None, url=None, web_app=None):
                self.text = text
                self.callback_data = callback_data
                self.url = url
                self.web_app = web_app

        class _InlineKeyboardMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard

        class _ReplyKeyboardMarkup:
            def __init__(self, keyboard=None, **kwargs):
                self.keyboard = keyboard or []
                self.kwargs = kwargs

        class _KeyboardButton:
            def __init__(self, text):
                self.text = text

        class _ReplyKeyboardRemove:
            pass

        class _BotCommand:
            def __init__(self, command, description):
                self.command = command
                self.description = description

        # Mirror PTB's hierarchy: BadRequest is a TelegramError, so handlers that
        # catch the base class also catch the specific one.
        class _TelegramError(Exception):
            pass

        class _BadRequest(_TelegramError):
            pass

        telegram_constants.ParseMode = _ParseMode
        telegram_constants.ChatAction = _ChatAction
        telegram_ext.CallbackContext = _CallbackContext

        class _ContextTypes:
            DEFAULT_TYPE = _CallbackContext

        telegram_ext.ContextTypes = _ContextTypes
        telegram_mod.Update = _Update
        telegram_mod.InlineKeyboardButton = _InlineKeyboardButton
        telegram_mod.InlineKeyboardMarkup = _InlineKeyboardMarkup
        telegram_mod.ReplyKeyboardMarkup = _ReplyKeyboardMarkup
        telegram_mod.KeyboardButton = _KeyboardButton
        telegram_mod.ReplyKeyboardRemove = _ReplyKeyboardRemove
        telegram_mod.BotCommand = _BotCommand
        telegram_mod.WebAppInfo = _WebAppInfo
        telegram_error.TelegramError = _TelegramError
        telegram_error.BadRequest = _BadRequest

        sys.modules["telegram"] = telegram_mod
        sys.modules["telegram.constants"] = telegram_constants
        sys.modules["telegram.ext"] = telegram_ext
        sys.modules["telegram.error"] = telegram_error

    # apscheduler
    if "apscheduler" not in sys.modules:
        apscheduler_mod = types.ModuleType("apscheduler")
        apscheduler_schedulers = types.ModuleType("apscheduler.schedulers")
        apscheduler_asyncio = types.ModuleType("apscheduler.schedulers.asyncio")

        class _AsyncIOScheduler:
            def __init__(self, *args, **kwargs):
                self.running = False
                self.jobs = []

            def add_job(self, *args, **kwargs):
                self.jobs.append((args, kwargs))

            def start(self):
                self.running = True

            def shutdown(self, wait=True):
                self.running = False

            def get_jobs(self):
                return list(self.jobs)

        apscheduler_asyncio.AsyncIOScheduler = _AsyncIOScheduler
        sys.modules["apscheduler"] = apscheduler_mod
        sys.modules["apscheduler.schedulers"] = apscheduler_schedulers
        sys.modules["apscheduler.schedulers.asyncio"] = apscheduler_asyncio

    # psycopg2
    if "psycopg2" not in sys.modules:
        psycopg2_mod = types.ModuleType("psycopg2")
        psycopg2_pool = types.ModuleType("psycopg2.pool")
        psycopg2_extras = types.ModuleType("psycopg2.extras")
        psycopg2_sql = types.ModuleType("psycopg2.sql")

        class _ThreadedConnectionPool:
            def __init__(self, *args, **kwargs):
                pass

        class _RealDictCursor:
            pass

        # Exception hierarchy mirroring the real driver — db.py references
        # OperationalError/InterfaceError at import time for disconnect
        # detection.
        class _Error(Exception):
            pass

        class _InterfaceError(_Error):
            pass

        class _DatabaseError(_Error):
            pass

        class _OperationalError(_DatabaseError):
            pass

        psycopg2_mod.Error = _Error
        psycopg2_mod.InterfaceError = _InterfaceError
        psycopg2_mod.DatabaseError = _DatabaseError
        psycopg2_mod.OperationalError = _OperationalError

        class _SqlFragment:
            def __init__(self, value=""):
                self.value = value

            def format(self, *args, **kwargs):
                return self

            def join(self, _iterable):
                return self

            def __mul__(self, _):
                return self

        def _sql_factory(value=""):
            return _SqlFragment(value)

        def _identifier_factory(_value=""):
            return _SqlFragment("")

        def _placeholder_factory():
            return _SqlFragment("%s")

        psycopg2_pool.ThreadedConnectionPool = _ThreadedConnectionPool
        psycopg2_extras.RealDictCursor = _RealDictCursor
        psycopg2_sql.SQL = _sql_factory
        psycopg2_sql.Identifier = _identifier_factory
        psycopg2_sql.Placeholder = _placeholder_factory

        psycopg2_mod.pool = psycopg2_pool
        psycopg2_mod.extras = psycopg2_extras
        psycopg2_mod.sql = psycopg2_sql

        sys.modules["psycopg2"] = psycopg2_mod
        sys.modules["psycopg2.pool"] = psycopg2_pool
        sys.modules["psycopg2.extras"] = psycopg2_extras
        sys.modules["psycopg2.sql"] = psycopg2_sql

    # requests
    if "requests" not in sys.modules:
        requests_mod = types.ModuleType("requests")
        requests_adapters = types.ModuleType("requests.adapters")

        class _DummyResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {}

        class _DummySession:
            def __init__(self):
                self.headers = {}

            def mount(self, *args, **kwargs):
                return None

            def get(self, *args, **kwargs):
                return _DummyResponse()

            def post(self, *args, **kwargs):
                return _DummyResponse()

        def _requests_get(*args, **kwargs):
            return _DummyResponse()

        def _requests_post(*args, **kwargs):
            return _DummyResponse()

        class _HTTPAdapter:
            def __init__(self, *args, **kwargs):
                pass

        requests_mod.Session = _DummySession
        requests_mod.get = _requests_get
        requests_mod.post = _requests_post
        requests_mod.RequestException = Exception
        requests_mod.HTTPError = Exception
        requests_mod.Timeout = TimeoutError
        requests_adapters.HTTPAdapter = _HTTPAdapter
        sys.modules["requests"] = requests_mod
        sys.modules["requests.adapters"] = requests_adapters

    # eth_account
    if "eth_account" not in sys.modules:
        eth_account_mod = types.ModuleType("eth_account")

        class _Account:
            @staticmethod
            def enable_unaudited_hdwallet_features():
                return None

            @staticmethod
            def create():
                key = types.SimpleNamespace(hex=lambda: "0x0")
                return types.SimpleNamespace(key=key, address="0x" + "0" * 40)

            @staticmethod
            def from_key(_private_key):
                return types.SimpleNamespace(address="0x" + "0" * 40)

        eth_account_mod.Account = _Account
        sys.modules["eth_account"] = eth_account_mod

    # httpx — prefer the real package when it's installed. The stub below only
    # defines AsyncClient/HTTPStatusError; installing it unconditionally (when
    # httpx merely hasn't been imported *yet*) poisons sys.modules so a later
    # ``from httpx import URL, Proxy, Timeout, ...`` (openai does this, pulled in
    # via knowledge_service) fails at collection. Only fall back to the stub if
    # the real httpx genuinely isn't importable.
    if "httpx" not in sys.modules:
        try:
            import httpx  # noqa: F401  (registers the real module in sys.modules)
        except ImportError:
            _install_httpx_stub()


def _install_httpx_stub():
    if "httpx" not in sys.modules:
        httpx_mod = types.ModuleType("httpx")

        class _HttpxResponse:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def json(self):
                return {}

        class _AsyncClient:
            def __init__(self, *args, **kwargs):
                self.is_closed = False

            async def request(self, *args, **kwargs):
                return _HttpxResponse()

            async def aclose(self):
                self.is_closed = True

        class _HTTPStatusError(Exception):
            def __init__(self, *args, response=None, **kwargs):
                super().__init__(*args)
                self.response = response or _HttpxResponse()

        httpx_mod.AsyncClient = _AsyncClient
        httpx_mod.HTTPStatusError = _HTTPStatusError
        sys.modules["httpx"] = httpx_mod
