import atexit
import itertools
import logging
import pickle
import threading
from types import SimpleNamespace

import pytest

from efb_wechat_slave.vendor.wxpy.api import bot as bot_module
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


def official_account(*args, **kwargs):
    return MP(raw_chat(*args, **kwargs), None)


def raw_group(nick_name, members=()):
    raw = raw_chat(nick_name)
    raw["UserName"] = "@" + raw["UserName"]
    raw["MemberList"] = list(members)
    return raw


def group(nick_name):
    return Group(raw_group(nick_name), None)


def group_member(*args, **kwargs):
    return Member(raw_chat(*args, **kwargs), group("Some group"))


def plain_user(*args, **kwargs):
    return User(raw_chat(*args, **kwargs), None)


def as_group_member(chat):
    """The same account as it shows up in a group."""
    return Member(dict(chat.raw), group("Some group"))


def new_puid_of(chat):
    """The ID a chat gets when it is not recognised as a known one."""
    return chat.user_name[-8:]


@pytest.fixture(autouse=True)
def scheduled_dumps(monkeypatch):
    """
    Keep the maps created by a test from saving themselves in the background
    or at exit; a test saves explicitly instead. Yields the maps that asked
    for a save, one entry per request.
    """
    created = []
    requests = []
    original_init = PuidMap.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    logger = logging.getLogger(puid_map_module.__name__)
    handlers = list(logger.handlers)
    monkeypatch.setattr(PuidMap, "__init__", init)
    monkeypatch.setattr(PuidMap, "activate_dump", lambda self: requests.append(self))

    yield requests

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

    def start(puid_logs=None):
        if sessions:
            sessions[-1].dump()
        sessions.append(PuidMap(path, puid_logs))
        return sessions[-1]

    return start


LOOKUPS = ["contact list", "found later"]


def identify(puid_map, chat, lookup):
    """
    Look up a chat that is either part of the contact list matched at login,
    or only shows up after that.
    """
    puid_map.reconcile([chat] if lookup == "contact list" else [])
    return puid_map.get_puid(chat)


@pytest.mark.parametrize("lookup", LOOKUPS)
@pytest.mark.parametrize("make_chat", [friend, official_account])
def test_changed_signature_keeps_the_id_after_logging_in_again(login, lookup, make_chat):
    before = make_chat("Weather", 1, "Guangdong", "Shenzhen", "Forecasts every morning")
    puid = login().get_puid(before)

    after = make_chat("Weather", 1, "Guangdong", "Shenzhen", "Forecasts every evening")

    assert identify(login(), after, lookup) == puid


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_changed_region_keeps_the_id_after_logging_in_again(login, lookup):
    before = friend("Alice", 2, "Guangdong", "Shenzhen", "Hello")
    puid = login().get_puid(before)

    after = friend("Alice", 2, "Hong Kong", "Kowloon", "Hello")

    assert identify(login(), after, lookup) == puid


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_two_changed_values_keep_the_id(login, lookup):
    before = friend("Alice", 2, "Guangdong", "Shenzhen", "Hello")
    puid = login().get_puid(before)

    after = friend("Alice", 2, "Guangdong", "Guangzhou", "Good night")

    assert identify(login(), after, lookup) == puid


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_value_no_longer_shown_does_not_count_as_changed(login, lookup):
    before = friend("Alice", 2, "Guangdong", "Shenzhen", "Hello")
    puid = login().get_puid(before)

    after = friend("Alice", 2, "Beijing", "Haidian", "")

    assert identify(login(), after, lookup) == puid


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_three_changed_values_give_a_new_id(login, lookup):
    before = friend("Alice", 2, "Guangdong", "Shenzhen", "Hello")
    old = login().get_puid(before)

    after = friend("Alice", 2, "Beijing", "Haidian", "Good night")

    assert identify(login(), after, lookup) == new_puid_of(after)
    assert new_puid_of(after) != old


def test_changed_signature_is_remembered_for_the_next_login(login):
    puid = login().get_puid(friend("Alice", signature="Hello"))
    login().reconcile([friend("Alice", signature="Hello again")])

    # Only an exact match can identify a group member.
    assert login().get_puid(group_member("Alice", signature="Hello again")) == puid


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_unchanged_contact_keeps_the_id_even_with_a_value_missing(login, lookup):
    before = friend("Alice", 2, "Guangdong", "Shenzhen", "Hello")
    puid = login().get_puid(before)

    after = friend("Alice", 2, None, "Shenzhen", "Hello")

    assert identify(login(), after, lookup) == puid


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_remark_name_keeps_the_id_whatever_else_changed(login, lookup):
    before = friend("Old name", 1, "Guangdong", "Shenzhen", "Hello", remark_name="Boss")
    puid = login().get_puid(before)

    after = friend("New name", 2, "Beijing", "Haidian", "Bye", remark_name="Boss")

    assert identify(login(), after, lookup) == puid


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_first_stored_of_several_exact_matches_wins(login, lookup):
    first_session = login()
    first = first_session.get_puid(friend("Alice", 2, "Guangdong"))
    second = first_session.get_puid(friend("Alice", 2, "Beijing"))
    assert first != second

    # Has no province, so it conflicts with neither of the stored contacts.
    after = friend("Alice", 2)

    assert identify(login(), after, lookup) == first


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_changed_contact_never_takes_the_id_of_a_namesake(login, order):
    first_session = login()
    tea = first_session.get_puid(friend("Momo", signature="Tea lover"))
    news = first_session.get_puid(friend("Momo", signature="Daily market news"))
    assert tea != news

    # The changed signature is closer to the one of the other contact.
    tea_now = friend("Momo", signature="Daily market news and tea")
    news_now = friend("Momo", signature="Daily market news")
    chats = [tea_now, news_now]

    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert puid_map.get_puid(tea_now) == tea
    assert puid_map.get_puid(news_now) == news


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_changed_namesakes_each_keep_their_own_id(login, order):
    first_session = login()
    tea = first_session.get_puid(friend("Momo", signature="Tea lover"))
    news = first_session.get_puid(friend("Momo", signature="Daily market news"))

    tea_now = friend("Momo", signature="Tea lover since 2010")
    news_now = friend("Momo", signature="Daily market news, on weekdays")
    chats = [tea_now, news_now]

    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert puid_map.get_puid(tea_now) == tea
    assert puid_map.get_puid(news_now) == news


def save_namesakes(puid_map, named_last):
    """
    Save two contacts called Momo under separate IDs, the first with a full
    profile and the second with an empty one, which conflicts with no other
    profile. Returns their IDs.
    """
    known = [friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover"), friend("Momo")]
    # Contacts get separate IDs while their names differ. The one that took
    # the shared name last is also the one saved last.
    known[named_last].raw["NickName"] = "Mo"
    puids = [puid_map.get_puid(chat) for chat in known]
    known[named_last].raw["NickName"] = "Momo"
    assert [puid_map.get_puid(chat) for chat in known] == puids
    assert puids[0] != puids[1]
    return puids


NAMESAKE_ORDERS = pytest.mark.parametrize("order, named_last", [((0, 1), 0), ((0, 1), 1), ((1, 0), 0), ((1, 0), 1)])


@NAMESAKE_ORDERS
def test_namesakes_each_keep_their_own_id_when_one_has_an_empty_profile(login, order, named_last):
    detailed, plain = save_namesakes(login(), named_last)

    chats = [friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover"), friend("Momo")]
    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert [puid_map.get_puid(chat) for chat in chats] == [detailed, plain]


@NAMESAKE_ORDERS
def test_changed_namesake_and_one_with_an_empty_profile_each_keep_their_own_id(login, order, named_last):
    detailed, plain = save_namesakes(login(), named_last)

    chats = [friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover since 2010"), friend("Momo")]
    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert [puid_map.get_puid(chat) for chat in chats] == [detailed, plain]


@NAMESAKE_ORDERS
def test_namesake_changed_too_much_does_not_take_the_id_of_one_with_an_empty_profile(login, order, named_last):
    detailed, plain = save_namesakes(login(), named_last)

    chats = [friend("Momo", 1, "Beijing", "Haidian", "Coffee first"), friend("Momo")]
    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert [puid_map.get_puid(chat) for chat in chats] == [new_puid_of(chats[0]), plain]
    assert new_puid_of(chats[0]) != detailed


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_new_namesake_with_an_empty_profile_does_not_take_the_id_of_a_contact_with_a_remark_name(login, order):
    boss = login().get_puid(friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover", remark_name="Boss"))

    boss_now = friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover", remark_name="Boss")
    stranger = friend("Momo")
    chats = [boss_now, stranger]
    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert puid_map.get_puid(boss_now) == boss
    assert puid_map.get_puid(stranger) == new_puid_of(stranger)

    # Each of them is recognised on the next login.
    boss_later = friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover", remark_name="Boss")
    stranger_later = friend("Momo")
    chats = [boss_later, stranger_later]
    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert puid_map.get_puid(boss_later) == boss
    assert puid_map.get_puid(stranger_later) == new_puid_of(stranger)


@pytest.mark.parametrize("abandoned, others", [
    # Each of the others shows a part of the profile of the known contact.
    # Under one ID they would show all of it.
    (None, [("Momo", 1, "Guangdong"), ("Momo", 1, None, "Shenzhen", "Tea lover")]),
    # The other one fits an abandoned profile, and would complete it.
    (("Momo", 1, "Guangdong"), [("Momo", 1, None, "Shenzhen", "Tea lover")]),
    # The other one could be the abandoned contact with a new province.
    (("Momo", 1, "Beijing", "Shenzhen", "Tea lover"), [("Momo", 1, "Guangdong", None, "Tea lover")]),
])
def test_namesakes_that_come_to_look_like_a_known_contact_do_not_take_over_its_id(login, abandoned, others):
    first_session = login()
    known = first_session.get_puid(friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover"))
    if abandoned:
        # Saved under another name first, as it could share the ID otherwise.
        gone = friend("Mo", *abandoned[1:])
        old = first_session.get_puid(gone)
        gone.raw["NickName"] = "Momo"
        assert first_session.get_puid(gone) == old
        assert old != known

    def contact_list():
        return [friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover")] + [friend(*other) for other in others]

    login().reconcile(contact_list())

    chats = contact_list()
    puid_map = login()
    puid_map.reconcile(chats)
    puids = [puid_map.get_puid(chat) for chat in chats]

    assert puids[0] == known
    assert known not in puids[1:]


def test_contacts_that_fit_the_same_saved_profile_share_its_id_as_before(login):
    known = login().get_puid(friend("Alice", 2, "Guangdong", "Shenzhen"))

    # Neither shows a city, so neither conflicts with the saved profile.
    chats = [friend("Alice", 2, "Guangdong"), friend("Alice", 2, "Guangdong")]
    puid_map = login()
    puid_map.reconcile(chats)

    assert [puid_map.get_puid(chat) for chat in chats] == [known, known]


def test_groups_with_the_same_name_share_an_id_on_every_login(login):
    family = login().get_puid(group("Family"))

    for _ in range(2):
        groups = [group("Family"), group("Family")]
        puid_map = login()
        puid_map.reconcile(groups)

        assert [puid_map.get_puid(chat) for chat in groups] == [family, family]


@pytest.mark.parametrize("contacts, list_orders, lookup_orders", [
    # Two groups and an official account with the same name. The groups
    # swap places in the contact list from one login to the next.
    ([lambda: group("Family"), lambda: group("Family"),
      lambda: official_account("Family", 2, "", "", "")],
     [(0, 1, 2), (1, 0, 2)], [(0, 1, 2), (1, 0, 2)]),
    # An official account, and friends of the same name that have remark
    # names and hardly any profile.
    ([lambda: official_account("Momo", 2, "Guangdong", "Shenzhen", "Daily news"),
      lambda: friend("Momo", 0, None, "", "", remark_name="Anna"),
      lambda: friend("Momo", None, None, "", None, remark_name="Ben"),
      lambda: friend("Momo", None, None, "", "", remark_name="Chris")],
     [(0, 1, 2, 3)], [(0, 1, 2, 3)]),
    # Three namesakes, looked up in another order in every session.
    ([lambda: friend("Momo", 0, "Beijing", "", "Daily news"),
      lambda: official_account("Momo", 0, "Guangdong", "Shenzhen", ""),
      lambda: friend("Momo", 0, "", "Shenzhen", "")],
     [(0, 1, 2)], [(0, 1, 2), (2, 1, 0), (1, 2, 0), (2, 0, 1)]),
])
def test_namesakes_that_shared_ids_have_the_same_ids_on_every_login(login, contacts, list_orders, lookup_orders):
    # Looked up one by one, namesakes with profiles that do not conflict
    # end up under one ID.
    first_session = login()
    for make_chat in contacts:
        first_session.get_puid(make_chat())

    sessions = []
    for number in range(6):
        chats = [make_chat() for make_chat in contacts]
        puid_map = login()
        puid_map.reconcile([chats[i] for i in list_orders[number % len(list_orders)]])
        found = {i: puid_map.get_puid(chats[i]) for i in lookup_orders[number % len(lookup_orders)]}
        puids = [found[i] for i in range(len(chats))]

        # Asked again in the same session, each chat still has the same ID.
        assert [puid_map.get_puid(chat) for chat in chats] == puids
        sessions.append(puids)

    assert sessions[1:] == sessions[:-1]


def test_looking_up_unchanged_contacts_after_login_leaves_nothing_to_save(login, scheduled_dumps):
    chats = [group("Family"), group("Family"), friend("Alice", remark_name="Boss")]
    puid_map = login()
    puid_map.reconcile(chats)
    assert scheduled_dumps == [puid_map]

    puids = [puid_map.get_puid(chat) for chat in chats + chats[::-1]]

    # The two groups share an ID, which only remembers one of them at a time.
    assert puids[0] == puids[1]
    assert scheduled_dumps == [puid_map]


@pytest.mark.parametrize("change, seen_next_time", [
    # Only an exact match can identify a group member.
    ({"Signature": "Hello again"}, lambda: group_member("Alice", signature="Hello again")),
    ({"RemarkName": "Boss"}, lambda: friend("Renamed", remark_name="Boss")),
])
def test_change_during_a_session_is_remembered_for_the_next_login(login, change, seen_next_time):
    puid = login().get_puid(friend("Alice", signature="Hello"))

    alice = friend("Alice", signature="Hello")
    puid_map = login()
    puid_map.reconcile([alice])
    alice.raw.update(change)

    assert puid_map.get_puid(alice) == puid
    assert login().get_puid(seen_next_time()) == puid


@pytest.mark.parametrize("seen_as", [lambda chat: chat, as_group_member])
@pytest.mark.parametrize("change, seen_next_time", [
    # Only an exact match can identify a group member.
    ({"Province": "Beijing", "City": "Haidian", "Signature": "Good night"},
     lambda: group_member("Alice", 2, "Guangdong", "Shenzhen", "Hello")),
    ({"RemarkName": "Chief"}, lambda: friend("Renamed", remark_name="Boss")),
])
def test_change_undone_during_a_session_is_not_remembered_for_the_next_login(login, change, seen_next_time, seen_as):
    profile = ("Alice", 2, "Guangdong", "Shenzhen", "Hello")
    puid = login().get_puid(friend(*profile, remark_name="Boss"))

    alice = friend(*profile, remark_name="Boss")
    puid_map = login()
    puid_map.reconcile([alice])
    alice = seen_as(alice)
    before = dict(alice.raw)

    alice.raw.update(change)
    assert puid_map.get_puid(alice) == puid
    alice.raw.update(before)
    assert puid_map.get_puid(alice) == puid

    assert login().get_puid(seen_next_time()) == puid


def test_seeing_a_contact_as_a_group_member_does_not_change_which_contact_a_shared_id_remembers(login):
    # Their profiles are the same, so they share an ID. It can only remember
    # one remark name: the one of the contact recorded last.
    anna, ben = friend("Momo", remark_name="Anna"), friend("Momo", remark_name="Ben")
    puid_map = login()
    puid_map.reconcile([anna, ben])
    shared = puid_map.get_puid(anna)
    assert puid_map.get_puid(ben) == shared

    # In a group, the same account shows no remark name.
    anna_in_group = as_group_member(anna)
    anna_in_group.raw["RemarkName"] = None
    for chat in (anna_in_group, anna, anna_in_group, anna):
        assert puid_map.get_puid(chat) == shared

    assert login().get_puid(friend("Renamed", remark_name="Ben")) == shared


def test_contact_that_changes_during_a_session_keeps_its_id(login):
    boss = login().get_puid(friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover", remark_name="Boss"))

    boss_now = friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover", remark_name="Boss")
    # New contacts with empty profiles: they share a new ID.
    strangers = [friend("Momo"), friend("Momo")]
    puid_map = login()
    puid_map.reconcile([boss_now] + strangers)
    shared = puid_map.get_puid(strangers[0])
    assert shared != boss
    assert puid_map.get_puid(strangers[1]) == shared

    # Looked up on its own, this profile would fit the one saved first.
    strangers[0].raw["City"] = "Shenzhen"

    assert puid_map.get_puid(strangers[0]) == shared
    assert puid_map.get_puid(boss_now) == boss


def test_contacts_are_looked_up_as_before_when_matching_the_contact_list_failed(login, scheduled_dumps):
    family = login().get_puid(group("Family"))
    del scheduled_dumps[:]

    groups = [group("Family"), group("Family")]
    puid_map = login()
    with pytest.raises(TypeError):
        puid_map.reconcile(groups + contact_list_with_unusable_profile())
    assert scheduled_dumps == []

    # Each lookup records the chat, so that what was matched before the
    # failure gets saved.
    assert [puid_map.get_puid(chat) for chat in groups] == [family, family]
    assert scheduled_dumps == [puid_map, puid_map]


def test_namesakes_saved_by_an_older_version_each_keep_their_own_id(tmp_path):
    path = tmp_path / "wxpy_puid.pkl"
    captions = TwoWayDict()
    captions[("Momo", 1, "Guangdong", "Shenzhen")] = "de7a11ed"
    captions[("Momo", 0, "", "")] = "00b1a9c0"
    with open(path, "wb") as f:
        pickle.dump((TwoWayDict(), TwoWayDict(), TwoWayDict(), captions), f)

    detailed = friend("Momo", 1, "Guangdong", "Shenzhen", "")
    plain = friend("Momo", 0, "", "", "")
    puid_map = PuidMap(path)
    puid_map.reconcile([plain, detailed])

    assert puid_map.get_puid(detailed) == "de7a11ed"
    assert puid_map.get_puid(plain) == "00b1a9c0"


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_new_namesake_does_not_take_the_id_of_an_unchanged_contact(login, order):
    known = login().get_puid(friend("Momo", signature="Tea lover"))

    known_now = friend("Momo", signature="Tea lover")
    stranger = friend("Momo", signature="Tea lover, too")
    chats = [known_now, stranger]

    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert puid_map.get_puid(known_now) == known
    assert puid_map.get_puid(stranger) == new_puid_of(stranger)


def test_id_in_use_in_this_session_is_not_given_to_another_chat(login):
    known = login().get_puid(friend("Momo", signature="Tea lover"))

    puid_map = login()
    puid_map.reconcile([])
    known_now = friend("Momo", signature="Tea lover")
    assert puid_map.get_puid(known_now) == known

    stranger = friend("Momo", signature="Tea lover, too")

    assert puid_map.get_puid(stranger) == new_puid_of(stranger)
    assert puid_map.get_puid(known_now) == known


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
@pytest.mark.parametrize("remark_names", [(None, None), ("Boss", "Client")])
def test_contact_added_during_a_session_does_not_take_the_id_of_a_namesake(login, order, remark_names):
    # Neither profile has a value that conflicts with the other one.
    profiles = [("Momo", 1, "Guangdong"), ("Momo", 1, None, "Shenzhen", "Tea lover")]

    def contacts():
        return [friend(*profile, remark_name=remark_name) for profile, remark_name in zip(profiles, remark_names)]

    known = login().get_puid(contacts()[0])

    known_now, added = contacts()
    puid_map = login()
    puid_map.reconcile([known_now])

    assert puid_map.get_puid(added) == new_puid_of(added)
    assert new_puid_of(added) != known
    assert puid_map.get_puid(known_now) == known
    assert puid_map.get_puid(added) == new_puid_of(added)

    # On the next login both are part of the contact list.
    chats = contacts()
    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert [puid_map.get_puid(chat) for chat in chats] == [known, new_puid_of(added)]


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
@pytest.mark.parametrize("known_as, added_as", [
    # The same remark name.
    ((("Momo", 1), "Boss"), (("Coco", 2), "Boss")),
    # The same profile.
    ((("Momo", 1, "Guangdong"), None), (("Momo", 1, "Guangdong"), None)),
    # The same profile, and a remark name that only the added contact has.
    ((("Momo", 1, "Guangdong"), None), (("Momo", 1, "Guangdong"), "Client")),
])
def test_contact_added_during_a_session_does_not_take_the_id_of_one_with_the_same_remark_name_or_profile(
        login, order, known_as, added_as):
    def contacts():
        return [friend(*profile, remark_name=remark_name) for profile, remark_name in (known_as, added_as)]

    known = login().get_puid(contacts()[0])

    known_now, added = contacts()
    puid_map = login()
    puid_map.reconcile([known_now])

    assert puid_map.get_puid(added) == new_puid_of(added)
    assert new_puid_of(added) != known
    assert puid_map.get_puid(known_now) == known

    # On the next login both are part of the contact list.
    chats = contacts()
    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert puid_map.get_puid(chats[0]) == known


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
@pytest.mark.parametrize("remark_name", [None, "Boss"])
def test_contact_that_stopped_showing_a_value_keeps_its_id_after_a_namesake_showing_the_same_was_added(
        login, scheduled_dumps, order, remark_name):
    known = login().get_puid(friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover", remark_name=remark_name))

    def contacts():
        # The long-time contact no longer shows its signature; the added one has none.
        return [friend("Momo", 1, "Guangdong", "Shenzhen", "", remark_name=remark_name),
                friend("Momo", 1, "Guangdong", "Shenzhen", "")]

    known_now, added = contacts()
    puid_map = login()
    puid_map.reconcile([known_now])

    assert puid_map.get_puid(added) == new_puid_of(added)
    assert new_puid_of(added) != known
    # Looking them up again changes nothing that would have to be saved.
    scheduled_dumps.clear()
    assert puid_map.get_puid(known_now) == known
    assert puid_map.get_puid(added) == new_puid_of(added)
    assert scheduled_dumps == []

    # On the next login both are part of the contact list.
    chats = contacts()
    puid_map = login()
    puid_map.reconcile([chats[i] for i in order])

    assert puid_map.get_puid(chats[0]) == known
    # Only a remark name tells the two apart. Without one they share the ID,
    # as they would have done all along.
    assert puid_map.get_puid(chats[1]) == (new_puid_of(added) if remark_name else known)


def test_contact_whose_remark_name_was_not_kept_is_not_moved_to_the_id_of_an_added_namesake(login):
    def contacts():
        return [friend("Momo", 1, "Guangdong", "", "Tea lover", remark_name="Anna"),
                friend("Momo", 1, "", "", "", remark_name="Ben")]

    # The two share an ID, which only remembers the remark name of one of them.
    first_session = login()
    shared = first_session.get_puid(contacts()[0])
    assert first_session.get_puid(contacts()[1]) == shared

    puid_map = login()
    puid_map.reconcile(contacts())
    # From here on it is Anna's.
    added = friend("Momo", 1, "", "", "")
    assert puid_map.get_puid(added) == new_puid_of(added)

    anna, ben = contacts()
    puid_map = login()
    puid_map.reconcile([anna, ben])

    assert puid_map.get_puid(anna) == shared
    assert puid_map.get_puid(ben) != new_puid_of(added)


def test_group_keeps_its_id_after_a_group_of_the_same_name_showed_up_during_a_session(login):
    first_session = login()
    known = first_session.get_puid(group("Family"))
    # A contact of the same name has shared the ID and left its region with it.
    assert first_session.get_puid(friend("Family", None, "Guangdong", "Shenzhen")) == known

    family = group("Family")
    puid_map = login()
    puid_map.reconcile([family])
    other = group("Family")
    assert puid_map.get_puid(other) == new_puid_of(other)
    assert puid_map.get_puid(family) == known

    family = group("Family")
    puid_map = login()
    puid_map.reconcile([family])

    assert puid_map.get_puid(family) == known


def test_group_named_like_a_contact_keeps_the_id_it_got_when_it_showed_up_during_a_session(login):
    known = login().get_puid(friend("News", 0, "", "", ""))

    def session():
        contact = friend("News", 0, "", "", "")
        puid_map = login()
        puid_map.reconcile([contact])
        return puid_map.get_puid(contact), puid_map.get_puid(group("News"))

    contact_id, group_id = session()
    assert contact_id == known
    assert group_id != known

    assert session() == (known, group_id)


@pytest.mark.parametrize("abandoned, added_as", [
    # The added contact fits an abandoned profile, and would complete it.
    (("Momo", 1, "Guangdong"), ("Momo", 1, None, "Shenzhen", "Tea lover")),
    # The added contact could be the abandoned one with a new province.
    (("Momo", 1, "Beijing", "Shenzhen", "Tea lover"), ("Momo", 1, "Guangdong", None, "Tea lover")),
])
def test_contact_added_during_a_session_does_not_come_to_look_like_a_known_contact_under_an_abandoned_id(
        login, abandoned, added_as):
    first_session = login()
    known = first_session.get_puid(friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover"))
    # Saved under another name first, as it could share the ID otherwise.
    gone = friend("Mo", *abandoned[1:])
    old = first_session.get_puid(gone)
    gone.raw["NickName"] = "Momo"
    assert first_session.get_puid(gone) == old
    assert old != known

    puid_map = login()
    puid_map.reconcile([friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover")])
    added = friend(*added_as)

    assert puid_map.get_puid(added) == new_puid_of(added)
    assert new_puid_of(added) not in (known, old)

    known_later = friend("Momo", 1, "Guangdong", "Shenzhen", "Tea lover")
    puid_map = login()
    puid_map.reconcile([known_later])

    assert puid_map.get_puid(known_later) == known


@pytest.mark.parametrize("make_chat", [group_member, plain_user])
def test_chat_outside_the_contact_list_shares_the_id_of_a_namesake_as_before(login, make_chat):
    known = login().get_puid(friend("Momo", 1, "Guangdong"))

    known_now = friend("Momo", 1, "Guangdong")
    puid_map = login()
    puid_map.reconcile([known_now])

    # Its profile has no value that conflicts with the one of the contact.
    assert puid_map.get_puid(make_chat("Momo", 1, None, "Shenzhen", "Tea lover")) == known
    assert puid_map.get_puid(known_now) == known


def test_contact_found_later_keeps_its_id_after_a_group_member_was_given_the_same_one(login):
    known = login().get_puid(friend("Momo", 1, "Guangdong"))

    puid_map = login()
    puid_map.reconcile([])
    known_now = friend("Momo", 1, "Guangdong")
    assert puid_map.get_puid(known_now) == known
    # From here on the ID remembers the group member instead of the contact.
    assert puid_map.get_puid(group_member("Momo", 1, None, "Shenzhen", "Tea lover")) == known

    assert puid_map.get_puid(known_now) == known


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_equally_similar_old_ids_give_a_new_id(login, lookup):
    first_session = login()
    old = {first_session.get_puid(friend("Alice", 2, "Guangdong", "Shenzhen")),
           first_session.get_puid(friend("Alice", 2, "Guangdong", "Guangzhou"))}
    assert len(old) == 2

    after = friend("Alice", 2, "Guangdong", "Zhuhai")

    assert identify(login(), after, lookup) == new_puid_of(after)
    assert new_puid_of(after) not in old


def test_old_id_equally_similar_to_two_contacts_goes_to_neither(login):
    old = login().get_puid(friend("Alice", 2, "Guangdong", "Shenzhen"))

    chats = [friend("Alice", 2, "Guangdong", "Guangzhou"),
             friend("Alice", 2, "Guangdong", "Zhuhai")]
    puid_map = login()
    puid_map.reconcile(chats)

    assert [puid_map.get_puid(chat) for chat in chats] == [new_puid_of(chat) for chat in chats]
    assert old not in map(new_puid_of, chats)


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_different_gender_gives_a_new_id(login, lookup):
    old = login().get_puid(friend("Sam", 1, "Guangdong", "Shenzhen", "Hello"))

    after = friend("Sam", 2, "Guangdong", "Shenzhen", "Hello")

    assert identify(login(), after, lookup) == new_puid_of(after)
    assert new_puid_of(after) != old


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_group_member_only_keeps_an_id_on_an_exact_match(login, lookup):
    old = login().get_puid(friend("Kim", 1, "Guangdong", "Shenzhen", "Hello"))

    changed = group_member("Kim", 1, "Guangdong", "Shenzhen", "Hello again")
    puid_map = login()

    assert identify(puid_map, changed, lookup) == new_puid_of(changed)
    assert new_puid_of(changed) != old

    unchanged = group_member("Kim", 1, "Guangdong", "Shenzhen", "Hello")

    assert identify(puid_map, unchanged, lookup) == old


@pytest.mark.parametrize("lookup", LOOKUPS)
@pytest.mark.parametrize("signature_now", ["Hello", "Hello again"])
def test_name_shown_for_a_closed_account_never_identifies_a_contact(login, lookup, signature_now):
    old = login().get_puid(friend("该账号已注销", 1, "Guangdong", "Shenzhen", "Hello"))

    # Unrelated accounts show this name once they are closed.
    after = friend("该账号已注销", 1, "Guangdong", "Shenzhen", signature_now)

    assert identify(login(), after, lookup) == new_puid_of(after)
    assert new_puid_of(after) != old


def test_old_id_that_lost_its_profile_to_another_contact_is_passed_over(login):
    first_session = login()
    boss = first_session.get_puid(friend("Alice", 2, "Guangdong", remark_name="Boss"))
    first_session.get_puid(friend("Alice", 2, "Beijing"))

    # The first contact now looks exactly like the second one used to.
    boss_now = friend("Alice", 2, "Beijing", remark_name="Boss")
    stranger = friend("Alice", 2, "Shanghai")
    puid_map = login()
    puid_map.reconcile([stranger, boss_now])

    assert puid_map.get_puid(boss_now) == boss
    assert puid_map.get_puid(stranger) == new_puid_of(stranger)


def test_group_keeps_the_id_and_system_account_is_left_alone(login):
    family = group("Family")
    puid = login().get_puid(family)

    file_helper = Friend({"UserName": "filehelper"}, None)
    family_now = group("Family")
    puid_map = login()
    puid_map.reconcile([file_helper, family_now])

    assert puid_map.get_puid(family_now) == puid
    assert puid_map.get_puid(file_helper) == "filehelper"
    assert "filehelper" not in puid_map.user_names


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


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_keeping_an_id_despite_changes_is_logged_with_both_versions(login, tmp_path, caplog, lookup):
    caplog.set_level(logging.DEBUG, logger=puid_map_module.__name__)
    puid = login().get_puid(friend("Weather", 1, "Guangdong", "Shenzhen", "Every morning"))

    log_path = tmp_path / "puid.log"
    after = friend("Weather", 1, "Guangdong", "Shenzhen", "Every evening")
    assert identify(login(str(log_path)), after, lookup) == puid

    old_caption = repr(("Weather", 1, "Guangdong", "Shenzhen", "Every morning"))
    new_caption = repr(("Weather", 1, "Guangdong", "Shenzhen", "Every evening"))
    assert any(puid in line and old_caption in line and new_caption in line
               for line in log_path.read_text(encoding="utf-8").splitlines())


def unfinished_contact_list():
    yield friend("Bob")
    raise RuntimeError("contact list unavailable")


def contact_list_with_unusable_profile():
    # The first contact is matched before the second one fails.
    return [friend("Alice", signature="Hello"), friend("Carol", signature=["not", "text"])]


@pytest.mark.parametrize("contact_list, error", [
    (None, None),
    (unfinished_contact_list, RuntimeError),
    (contact_list_with_unusable_profile, TypeError),
])
def test_changed_contact_gets_a_new_id_unless_the_contact_list_was_matched(login, tmp_path, caplog,
                                                                           contact_list, error):
    caplog.set_level(logging.DEBUG, logger=puid_map_module.__name__)
    first_session = login()
    unchanged = first_session.get_puid(friend("Alice", signature="Hello"))
    old = first_session.get_puid(friend("Weather", signature="Every morning"))

    log_path = tmp_path / "puid.log"
    puid_map = login(str(log_path))
    if contact_list:
        with pytest.raises(error):
            puid_map.reconcile(contact_list())
    changed = friend("Weather", signature="Every evening")

    assert puid_map.get_puid(changed) == new_puid_of(changed)
    assert new_puid_of(changed) != old
    assert puid_map.get_puid(friend("Alice", signature="Hello")) == unchanged
    assert any("Weather" in line and "only be matched exactly" in line
               for line in log_path.read_text(encoding="utf-8").splitlines())


def test_contact_list_is_saved_once(login, scheduled_dumps):
    puid_map = login()

    puid_map.reconcile([friend("Alice"), friend("Bob"), group("Family")])

    assert scheduled_dumps == [puid_map]


def test_unreadable_chat_does_not_stop_the_others(login):
    class Unreadable:
        @property
        def user_name(self):
            raise RuntimeError("no data for this chat")

    puid = login().get_puid(friend("Alice", signature="Hello"))

    after = friend("Alice", signature="Hello again")
    puid_map = login()
    puid_map.reconcile([Unreadable(), after])

    assert puid_map.get_puid(after) == puid


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


def logged_in_bot(friends=(), mps=(), groups=()):
    """A bot right after login, given the contact data it has stored."""
    bot = Bot.__new__(Bot)
    bot.core = Server(friends, mps, groups)
    bot.self = User(raw_chat("Me"), bot)
    bot.puid_map = None
    return bot


def test_enabling_ids_matches_the_whole_contact_list_first(login, tmp_path):
    first_session = login()
    tea = first_session.get_puid(friend("Momo", signature="Tea lover"))
    news = first_session.get_puid(official_account("Momo", signature="Daily market news"))
    family = first_session.get_puid(group("Family"))
    first_session.dump()

    # Looked up one by one in this order, the first chat would take the ID of the second.
    tea_now = raw_chat("Momo", signature="Daily market news and tea")
    news_now = raw_chat("Momo", signature="Daily market news")
    family_now = raw_group("Family")
    bot = logged_in_bot(friends=[tea_now], mps=[news_now], groups=[family_now])

    puid_map = bot.enable_puid(tmp_path / "wxpy_puid.pkl")

    assert bot.puid_map is puid_map
    assert puid_map.get_puid(Friend(tea_now, bot)) == tea
    assert puid_map.get_puid(MP(news_now, bot)) == news
    assert puid_map.get_puid(Group(family_now, bot)) == family


def test_enabling_ids_asks_the_server_for_nothing(login, tmp_path):
    first_session = login()
    alice = first_session.get_puid(friend("Alice", signature="Hello"))
    family = first_session.get_puid(group("Family"))
    first_session.dump()

    alice_now = raw_chat("Alice", signature="Hello again")
    # Its members have not been loaded yet.
    family_now = raw_group("Family")
    named_after_members = raw_group(None, members=[raw_chat("Alice"), raw_chat("Bob")])
    # Neither a name nor members: only the server can tell what it is called.
    nameless = raw_group(None)
    bot = logged_in_bot(friends=[alice_now], mps=[raw_chat("Weather")],
                        groups=[family_now, named_after_members, nameless])

    puid_map = bot.enable_puid(tmp_path / "wxpy_puid.pkl")

    assert bot.core.requests == []
    assert puid_map.get_puid(Friend(alice_now, bot)) == alice
    assert puid_map.get_puid(Group(family_now, bot)) == family
    assert named_after_members["UserName"] in puid_map.user_names
    assert nameless["UserName"] not in puid_map.user_names
    assert bot.core.requests == []


def test_enabling_ids_survives_a_failure_to_match_the_contact_list(login, tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger=bot_module.__name__)
    first_session = login()
    unchanged = first_session.get_puid(friend("Alice", signature="Hello"))
    old = first_session.get_puid(friend("Weather", signature="Every morning"))
    first_session.dump()

    unchanged_now = raw_chat("Alice", signature="Hello")
    changed_now = raw_chat("Weather", signature="Every evening")
    bot = logged_in_bot(friends=[unchanged_now, changed_now])
    bot.core.storageClass.chatroomList = None

    puid_map = bot.enable_puid(tmp_path / "wxpy_puid.pkl")

    assert bot.puid_map is puid_map
    assert any("failed to match the contact list" in record.getMessage() for record in caplog.records)
    # Without the whole contact list, only an unchanged contact is recognised.
    assert puid_map.get_puid(Friend(unchanged_now, bot)) == unchanged
    assert puid_map.get_puid(Friend(changed_now, bot)) == changed_now["UserName"][-8:]
    assert changed_now["UserName"][-8:] != old


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
