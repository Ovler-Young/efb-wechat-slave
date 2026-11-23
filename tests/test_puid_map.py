import atexit
import itertools
import logging
import pickle
import threading
from types import SimpleNamespace

import pytest

from efb_wechat_slave.vendor.wxpy.api.bot import Bot
from efb_wechat_slave.vendor.wxpy.api.chats import MP, Friend, Group, Member, User
from efb_wechat_slave.vendor.wxpy.utils import puid_map as puid_map_module
from efb_wechat_slave.vendor.wxpy.utils.puid_map import PuidMap, TwoWayDict

_serial = itertools.count(1)


def raw_chat(nick_name, sex=None, province=None, city=None, signature=None,
             remark_name=None):
    """Contact data as received on a login: the UserName is new every time."""
    return {
        "UserName": f"@{next(_serial):032x}",
        "NickName": nick_name,
        "Sex": sex,
        "Province": province,
        "City": city,
        "Signature": signature,
        "RemarkName": remark_name,
    }


def friend(*args, **kwargs):
    return Friend(raw_chat(*args, **kwargs), None)


def raw_group(nick_name, members=()):
    raw = raw_chat(nick_name)
    raw["UserName"] = "@" + raw["UserName"]
    raw["MemberList"] = list(members)
    return raw


def group(nick_name):
    return Group(raw_group(nick_name), None)


def group_member(*args, **kwargs):
    return Member(raw_chat(*args, **kwargs), group("Some group"))


def new_puid_of(chat):
    """The ID a chat gets when it is not recognised as a known one."""
    return chat.user_name[-8:]


@pytest.fixture(autouse=True)
def scheduled_dumps(monkeypatch):
    """
    Keep the maps created by a test from saving themselves in the background
    or at exit; a test saves explicitly instead.
    """
    created = []
    original_init = PuidMap.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    logger = logging.getLogger(puid_map_module.__name__)
    handlers = list(logger.handlers)
    monkeypatch.setattr(PuidMap, "__init__", init)
    monkeypatch.setattr(PuidMap, "activate_dump", lambda self: None)

    yield

    for puid_map in created:
        atexit.unregister(puid_map.dump)
    for handler in logger.handlers[:]:
        if handler not in handlers:
            logger.removeHandler(handler)
            handler.close()


@pytest.fixture
def login(tmp_path):
    """
    Start a login session: a new map on the same file, after the map of the
    previous session (if any) has been saved.
    """
    path = tmp_path / "wxpy_puid.pkl"
    sessions = []

    def start():
        if sessions:
            sessions[-1].dump()
        sessions.append(PuidMap(path))
        return sessions[-1]

    return start


@pytest.mark.parametrize("make_chat", [friend, group_member])
def test_chat_not_in_the_contact_list_at_login_is_matched_exactly(login, make_chat):
    old = login().get_puid(friend("Kim", 1, "Guangdong", "Shenzhen", "Hello"))

    puid_map = login()
    puid_map.reconcile([])
    changed = make_chat("Kim", 1, "Guangdong", "Shenzhen", "Hello again")

    assert puid_map.get_puid(changed) == new_puid_of(changed)
    assert new_puid_of(changed) != old
    # No conflicting value: the province is not shown.
    assert puid_map.get_puid(make_chat("Kim", 1, None, "Shenzhen", "Hello")) == old


@pytest.mark.parametrize("order", [tuple(range(8)), tuple(reversed(range(8))), (2, 4, 6, 1, 5, 7, 3, 0)])
def test_contact_list_is_matched_from_the_strongest_evidence_to_the_weakest(tmp_path, order):
    path = tmp_path / "wxpy_puid.pkl"
    remark_names, captions = TwoWayDict(), TwoWayDict()
    remark_names["Boss"] = "b0550000"
    captions[("Old name", 1, "Guangdong", None, None)] = "b0550000"
    captions[("Momo", None, None, None, "Tea lover")] = "7ea00000"
    captions[("Momo", None, None, None, "Daily market news")] = "2e550000"
    captions[("Alice", 2, "Guangdong", None, None)] = "a11ce000"
    captions[("Bob", 1, "Guangdong", None, None)] = "b0b00001"
    captions[("Bob", 1, "Guangdong", "Shenzhen", None)] = "b0b00002"
    captions[("Carol", None, None, None, None)] = "ca201000"
    captions[("Dave", 1, None, None, None)] = "da7e0000"
    captions[("Erin", 2, None, None, None)] = "e7140000"
    captions[("Erin", 2, "Guangdong", None, None)] = "e7142000"
    captions[("Carol", 2, "Guangdong", "Shenzhen", "Hello")] = "ca202000"
    with open(path, "wb") as f:
        pickle.dump((TwoWayDict(), TwoWayDict(), remark_names, captions), f)

    chats = [
        # Its profile is exactly the one saved for Dave.
        friend("Dave", 1, remark_name="Boss"),
        # Its changed signature is closer to the one of the other Momo, which is unchanged.
        friend("Momo", signature="Daily market news and tea"),
        friend("Momo", signature="Daily market news"),
        # No province, so it does not conflict with what is saved.
        friend("Alice", 2),
        # Exactly as saved for one Bob, and no conflicting value with the other one, saved first.
        friend("Bob", 1, "Guangdong", "Shenzhen"),
        # No conflicting value with the first Carol; the second one is closer but has another signature.
        friend("Carol", 2, "Guangdong", "Shenzhen", "Hello!"),
        # Both exactly as saved for the second Erin, and they keep sharing its ID.
        friend("Erin", 2, "Guangdong"),
        friend("Erin", 2, "Guangdong"),
    ]
    puid_map = PuidMap(path)
    puid_map.reconcile([chats[i] for i in order])

    assert [puid_map.get_puid(chat) for chat in chats] == [
        "b0550000", "7ea00000", "2e550000", "a11ce000", "b0b00002", "ca201000", "e7142000", "e7142000"]


@pytest.mark.parametrize("saved, now, kept", [
    ([("Weather", 1, "Guangdong", "Shenzhen", "Every morning")],
     ("Weather", 1, "Guangdong", "Shenzhen", "Every evening"), 0),
    ([("Alice", 2, "Guangdong", "Shenzhen", "Hello")], ("Alice", 2, "Hong Kong", "Kowloon", "Hello"), 0),
    ([("Alice", 2, "Guangdong", "Shenzhen", "Hello")], ("Alice", 2, "Beijing", "Haidian", ""), 0),
    ([("Alice", 2, "Guangdong", "Shenzhen", "Hello")], ("Alice", 2, "Beijing", "Haidian", "Good night"), None),
    ([("Sam", 1, "Guangdong", "Shenzhen", "Hello")], ("Sam", 2, "Guangdong", "Shenzhen", "Hello"), None),
    ([("Alice", 2, "Guangdong", "Shenzhen", None), ("Alice", 2, "Guangdong", "Guangzhou", None)],
     ("Alice", 2, "Guangdong", "Zhuhai", None), None),
])
def test_contact_whose_profile_changed_a_little_keeps_its_id(login, saved, now, kept):
    first_session = login()
    old = [first_session.get_puid(friend(*caption)) for caption in saved]

    after = friend(*now)
    puid_map = login()
    puid_map.reconcile([after])

    assert puid_map.get_puid(after) == (new_puid_of(after) if kept is None else old[kept])


def test_id_used_in_this_session_is_not_reused_for_a_changed_contact(login):
    old = login().get_puid(friend("Alice", 2, "Guangdong", "Shenzhen", "Hello"))

    puid_map = login()
    assert puid_map.get_puid(group_member("Alice", 2, "Guangdong", "Shenzhen", "Hello")) == old
    changed = friend("Alice", 2, "Guangdong", "Shenzhen", "Hello again")
    puid_map.reconcile([changed])

    assert puid_map.get_puid(changed) == new_puid_of(changed)


@pytest.mark.parametrize("signature_now", ["Hello", "Hello again"])
def test_name_shown_for_a_closed_account_never_identifies_a_contact(login, signature_now):
    old = login().get_puid(friend("该账号已注销", 1, "Guangdong", "Shenzhen", "Hello"))

    # Unrelated accounts show this name once they are closed.
    after = friend("该账号已注销", 1, "Guangdong", "Shenzhen", signature_now)
    puid_map = login()
    puid_map.reconcile([after])

    assert puid_map.get_puid(after) == new_puid_of(after)
    assert new_puid_of(after) != old


def test_map_saved_by_an_older_version_still_loads_and_keeps_its_layout(tmp_path):
    path = tmp_path / "wxpy_puid.pkl"
    user_names, captions = TwoWayDict(), TwoWayDict()
    user_names["@previous-alice"] = "a11ce000"
    user_names["@previous-bob"] = "b0b00000"
    captions[("Alice", 2, "Guangdong", "Shenzhen")] = "a11ce000"
    captions[("Bob", 1, "Guangdong", "Shenzhen")] = "b0b00000"
    with open(path, "wb") as f:
        pickle.dump((user_names, TwoWayDict(), TwoWayDict(), captions), f)

    alice = friend("Alice", 2, "Guangdong", "Shenzhen", "Hello")
    bob = friend("Bob", 1, "Guangdong", "Guangzhou", "Hi")
    puid_map = PuidMap(path)
    puid_map.reconcile([alice, bob])

    assert puid_map.get_puid(alice) == "a11ce000"
    assert puid_map.get_puid(bob) == "b0b00000"

    puid_map.dump()
    with open(path, "rb") as f:
        saved = pickle.load(f)

    assert isinstance(saved, tuple)
    assert [type(part) for part in saved] == [TwoWayDict] * 4
    assert saved[3].get_key("a11ce000") == ("Alice", 2, "Guangdong", "Shenzhen", "Hello")
    assert saved[3].get_key("b0b00000") == ("Bob", 1, "Guangdong", "Guangzhou", "Hi")


class Server:
    """
    Stands in for the connection to WeChat: holds the contacts stored at
    login, and records and refuses every request made to it.
    """

    def __init__(self, friends, mps, groups):
        self.storageClass = SimpleNamespace(updateLock=threading.Lock(), memberList=list(friends),
                                            mpList=list(mps), chatroomList=list(groups))
        self.requests = []

    def __getattr__(self, name):
        def request(*args, **kwargs):
            self.requests.append(name)
            raise ConnectionError(f"{name} is not available")

        return request


def test_enabling_ids_matches_the_stored_contact_list_without_asking_the_server(login, tmp_path):
    first_session = login()
    alice = first_session.get_puid(friend("Alice", signature="Hello"))
    weather = first_session.get_puid(MP(raw_chat("Weather", signature="Every morning"), None))
    family = first_session.get_puid(group("Family"))
    first_session.dump()

    alice_now = raw_chat("Alice", signature="Hello again")
    weather_now = raw_chat("Weather", signature="Every evening")
    # Its members have not been loaded yet.
    family_now = raw_group("Family")
    # Neither a name nor members: only the server can tell what it is called.
    nameless = raw_group(None)
    bot = Bot.__new__(Bot)
    bot.core = Server([alice_now], [weather_now], [family_now, nameless])
    bot.self = User(raw_chat("Me"), bot)
    bot.puid_map = None

    puid_map = bot.enable_puid(tmp_path / "wxpy_puid.pkl")

    assert bot.puid_map is puid_map
    assert bot.core.requests == []
    assert puid_map.get_puid(Friend(alice_now, bot)) == alice
    assert puid_map.get_puid(MP(weather_now, bot)) == weather
    assert puid_map.get_puid(Group(family_now, bot)) == family
    assert nameless["UserName"] not in puid_map.user_names
    assert bot.core.requests == []


def test_group_without_name_or_members_is_looked_up_without_waiting_on_itself(login):
    puid_map = login()
    nameless = raw_group(None)
    requests = []

    def update_chatroom(user_name, members_details=False):
        requests.append(user_name)
        return dict(nameless)

    bot = SimpleNamespace(puid_map=puid_map, core=SimpleNamespace(update_chatroom=update_chatroom))
    results = []
    lookup = threading.Thread(target=lambda: results.append(Group(dict(nameless), bot).puid), daemon=True)
    lookup.start()
    lookup.join(5)

    assert not lookup.is_alive()
    # Its name would come from its members, and it has none: no ID for now.
    assert results == [None]
    # The server is asked for its members once.
    assert requests == [nameless["UserName"]]
    assert nameless["UserName"] not in puid_map.user_names

    family = Group(raw_group("Family"), bot)
    assert family.puid == new_puid_of(family)
