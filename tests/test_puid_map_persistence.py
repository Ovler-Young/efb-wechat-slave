import pickle
from types import SimpleNamespace

import pytest

from efb_wechat_slave.vendor.wxpy.utils import puid_map as puid_map_module
from efb_wechat_slave.vendor.wxpy.utils.puid_map import PuidMap, TwoWayDict

PLACEHOLDER_NICK_NAMES = ["该账号已注销", "该账号已冻结", "账号已迁移"]


class ExitHooks:
    """Stands in for the interpreter's exit hooks, so an exit can be simulated."""

    def __init__(self):
        self.hooks = []

    def register(self, hook):
        self.hooks.append(hook)

    def unregister(self, hook):
        self.hooks = [registered for registered in self.hooks if registered != hook]

    def run(self):
        # Like the interpreter, run the hook registered last first.
        for hook in reversed(self.hooks):
            hook()


@pytest.fixture(autouse=True)
def exit_hooks(monkeypatch):
    hooks = ExitHooks()
    monkeypatch.setattr(puid_map_module, "atexit", hooks)
    return hooks


@pytest.fixture
def map_path(tmp_path):
    return tmp_path / "wxpy_puid.pkl"


@pytest.fixture
def open_map(map_path):
    """Open maps on the same file, and retire them all after the test."""
    maps = []

    def open_map():
        maps.append(PuidMap(str(map_path)))
        return maps[-1]

    yield open_map
    for opened in maps:
        opened.close()


def contact(user_name, nick_name, remark_name=None, sex=None, province=None,
            city=None, signature=None):
    return SimpleNamespace(user_name=user_name, wxid=None, nick_name=nick_name,
                           remark_name=remark_name, sex=sex, province=province,
                           city=city, signature=signature)


def wait_for_pending_dump(puid_map):
    """Let the automatic dump scheduled by a lookup, if any, write the file."""
    timer = puid_map._dump_task
    if timer is not None:
        timer.join(5)
        assert not timer.is_alive()


def test_profile_change_alone_is_saved_to_disk(open_map, monkeypatch):
    monkeypatch.setattr(PuidMap, "DUMP_TIMEOUT", 0.05)
    puid_map = open_map()
    friend = contact("@friend-00000001", "Alice", sex=2, province="Guangdong",
                     city="Shenzhen", signature="hello")
    puid = puid_map.get_puid(friend)
    wait_for_pending_dump(puid_map)

    # Looking the chat up again with nothing new to store saves nothing,
    # also when a part of its profile is not available this time.
    assert puid_map.get_puid(friend) == puid
    friend.province = None
    assert puid_map.get_puid(friend) == puid
    assert puid_map._dump_task is None

    friend.city = "Guangzhou"
    friend.signature = "gone fishing"
    assert puid_map.get_puid(friend) == puid
    wait_for_pending_dump(puid_map)

    saved = open_map()
    assert saved.captions.get_key(puid) == \
        ("Alice", 2, "Guangdong", "Guangzhou", "gone fishing")
    assert saved.user_names["@friend-00000001"] == puid


def test_chats_with_identical_profiles_do_not_keep_saving(open_map):
    puid_map = open_map()
    first = contact("@first-00000001", "Tom")
    second = contact("@second-00000002", "Jerry")
    first_puid = puid_map.get_puid(first)
    second_puid = puid_map.get_puid(second)
    assert first_puid != second_puid

    # The second chat takes the very profile of the first one: from now on
    # only one of them at a time can own it in the map.
    second.nick_name = "Tom"
    assert puid_map.get_puid(second) == second_puid

    scheduled = []
    puid_map.activate_dump = lambda: scheduled.append(True)
    for _ in range(3):
        assert puid_map.get_puid(first) == first_puid
        assert puid_map.get_puid(second) == second_puid
    assert scheduled == []

    # The first chat, which holds no entry right now, gets a profile of
    # its own again: that is a change to save.
    first.signature = "back soon"
    assert puid_map.get_puid(first) == first_puid
    assert scheduled == [True]


def test_replaced_map_never_overwrites_its_successor(
        open_map, map_path, exit_hooks, monkeypatch):
    old_map = open_map()
    old_map.get_puid(contact("@old-session-00000001", "Alice"))
    old_timer = old_map._dump_task
    assert old_timer is not None

    old_map.close()

    monkeypatch.setattr(PuidMap, "DUMP_TIMEOUT", 0.05)
    new_map = open_map()
    new_puid = new_map.get_puid(contact("@new-session-00000002", "Bob"))
    wait_for_pending_dump(new_map)
    saved = map_path.read_bytes()

    # The replaced map has nothing left that could write the file...
    assert old_timer.finished.is_set()
    assert old_map._dump_task is None
    assert exit_hooks.hooks == [new_map.dump]

    # ...and although it still answers lookups, it never saves them, not
    # even from a timer that had already fired when it was replaced.
    assert old_map.get_puid(contact("@old-session-00000003", "Carol")) == "00000003"
    old_map.activate_dump()
    assert old_map._dump_task is None
    old_map.dump()
    old_timer.function()
    assert map_path.read_bytes() == saved

    exit_hooks.run()
    after_exit = open_map()
    assert after_exit.user_names["@new-session-00000002"] == new_puid
    assert "@old-session-00000003" not in after_exit.user_names


def test_retiring_a_map_saves_changes_waiting_for_a_dump_once(open_map, map_path):
    puid_map = open_map()
    puid = puid_map.get_puid(contact("@friend-00000001", "Alice"))
    assert not map_path.exists()

    puid_map.close()
    puid_map.close()

    assert open_map().user_names["@friend-00000001"] == puid
    # A second write would have kept the first one as a backup.
    assert not map_path.with_name(map_path.name + ".bak").exists()


def test_retiring_a_map_with_nothing_to_save_writes_nothing(open_map, map_path):
    open_map().close()

    assert not map_path.exists()


def test_map_saved_by_an_earlier_version_still_loads(open_map, map_path):
    user_names, wxids, remark_names, captions = (TwoWayDict() for _ in range(4))
    user_names["@friend-00000001"] = "00000001"
    wxids["wxid_alice"] = "00000001"
    remark_names["Ali"] = "00000001"
    # Saved before signatures were part of the stored profile.
    captions[("Alice", 2, "Guangdong", "Shenzhen")] = "00000001"
    user_names["@friend-00000002"] = "00000002"
    captions[("Bob", 1, None, None, "hi")] = "00000002"
    map_path.write_bytes(
        pickle.dumps((user_names, wxids, remark_names, captions)))

    puid_map = open_map()

    assert dict(puid_map.user_names) == {"@friend-00000001": "00000001",
                                         "@friend-00000002": "00000002"}
    assert dict(puid_map.wxids) == {"wxid_alice": "00000001"}
    assert dict(puid_map.remark_names) == {"Ali": "00000001"}
    assert dict(puid_map.captions) == {
        ("Alice", 2, "Guangdong", "Shenzhen", None): "00000001",
        ("Bob", 1, None, None, "hi"): "00000002"}
    # In a new session the chats are recognised by their saved profiles.
    assert puid_map.get_puid(contact(
        "@relogin-00000009", "Bob", sex=1, signature="hi")) == "00000002"

    # What is saved back keeps the same layout.
    puid_map.close()
    saved = pickle.loads(map_path.read_bytes())
    assert isinstance(saved, tuple) and len(saved) == 4
    assert all(isinstance(part, TwoWayDict) for part in saved)
    assert saved[0]["@relogin-00000009"] == "00000002"


@pytest.mark.parametrize("nick_name", PLACEHOLDER_NICK_NAMES)
def test_accounts_sharing_a_placeholder_name_keep_separate_ids(open_map, nick_name):
    puid_map = open_map()
    first = contact("@closed-00000001", nick_name)
    second = contact("@closed-00000002", nick_name)

    first_puid = puid_map.get_puid(first)
    second_puid = puid_map.get_puid(second)

    assert first_puid != second_puid
    for _ in range(2):
        assert puid_map.get_puid(first) == first_puid
        assert puid_map.get_puid(second) == second_puid


@pytest.mark.parametrize("nick_name", PLACEHOLDER_NICK_NAMES)
def test_placeholder_name_does_not_identify_an_account_after_relogin(
        open_map, nick_name):
    old_map = open_map()
    first_puid = old_map.get_puid(contact("@closed-00000001", nick_name))
    second_puid = old_map.get_puid(
        contact("@closed-00000002", nick_name, remark_name="Old Wang"))
    old_map.close()

    # Logging in again loads the saved map and gives every chat a new user name.
    new_map = open_map()
    first_again = new_map.get_puid(contact("@relogin-00000003", nick_name))
    second_again = new_map.get_puid(
        contact("@relogin-00000004", nick_name, remark_name="Old Wang"))

    # Without anything else to tell them apart, the name alone must not
    # hand one account the ID of the other; a remark name still does.
    assert first_again not in (first_puid, second_puid)
    assert second_again == second_puid
