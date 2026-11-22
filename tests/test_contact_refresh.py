import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot import coordinator

import efb_wechat_slave
from efb_wechat_slave import WeChatChannel
from efb_wechat_slave.vendor.wxpy import MP, Friend, Group
from efb_wechat_slave.vendor.wxpy.utils import PuidMap

ONE_DAY = 24 * 60 * 60


class Session:
    """
    A stand-in for a logged-in session: it keeps a local copy of the contact
    list, which a full fetch replaces with what the server holds by then.
    """

    def __init__(self, puid_map=None, contacts=()):
        self.puid_map = puid_map
        self.alive = True
        self.local_contacts = list(contacts)
        self.server_contacts = list(contacts)
        self.core = SimpleNamespace(get_contact=Mock(side_effect=self._fetch))
        self.cleanup = Mock()

    def _fetch(self, update=False):
        self.local_contacts = list(self.server_contacts)

    def chats(self):
        return [chat_type(dict(raw), self)
                for chat_type, raw in self.local_contacts]


@pytest.fixture
def channel(monkeypatch, tmp_path):
    """A channel with no session yet, cut off from the master channel."""
    channel = WeChatChannel.__new__(WeChatChannel)
    channel._ = lambda text: text
    channel.logger = Mock()
    channel.flag = Mock(return_value=None)
    channel.qr_uuid = ("", 0)
    channel.done_reauth = threading.Event()
    channel.done_reauth.set()
    channel._stop_polling_event = threading.Event()
    channel.user_auth_chat = SimpleNamespace(other=object())
    channel.slave_message = Mock()
    channel.sent_to_master = []

    monkeypatch.setattr(efb_wechat_slave.efb_utils,
                        "get_data_path", lambda channel_id: tmp_path)
    monkeypatch.setattr(coordinator, "master", object(), raising=False)
    monkeypatch.setattr(coordinator, "send_message",
                        channel.sent_to_master.append)
    yield channel
    channel._cancel_contact_refresh()


@pytest.fixture
def puid_map(tmp_path):
    puid_map = PuidMap(str(tmp_path / "wxpy_puid.pkl"))
    yield puid_map
    puid_map.close()


def use_session(channel, session):
    """Put the channel in the state it has once logged in with the session."""
    channel.bot = session
    channel._schedule_contact_refresh(session)
    return channel._contact_refresh_timer


def log_in(channel, monkeypatch, session, first_start=False):
    monkeypatch.setattr(efb_wechat_slave.wxpy, "Bot", lambda **kwargs: session)
    channel.authenticate("console_qr_code", first_start=first_start)


def is_pending(timer):
    return timer.is_alive() and not timer.finished.is_set()


def test_refresh_stores_latest_profiles_without_changing_ids(channel, puid_map):
    friend = {"UserName": "@friend-00000001", "NickName": "Alice", "Sex": 2,
              "Province": "Guangdong", "City": "Shenzhen", "Signature": "hello",
              "RemarkName": "Ali"}
    group = {"UserName": "@@group-00000002", "NickName": "Family"}
    official = {"UserName": "@official-00000003", "NickName": "Daily News",
                "Signature": "News every day"}
    session = Session(puid_map, [(Friend, friend), (Group, group), (MP, official)])
    use_session(channel, session)
    puids = [chat.puid for chat in session.chats()]
    assert len(set(puids)) == 3
    user_names = dict(puid_map.user_names)

    # Since the login, all three have changed their profiles.
    session.server_contacts = [
        (Friend, dict(friend, City="Guangzhou", Signature="gone fishing")),
        (Group, dict(group, NickName="Big Family")),
        (MP, dict(official, Signature="News every hour"))]

    channel._refresh_contacts(session)

    session.core.get_contact.assert_called_once_with(update=True)
    assert [puid_map.captions.get_key(puid) for puid in puids] == [
        ("Alice", 2, "Guangdong", "Guangzhou", "gone fishing"),
        ("Big Family", None, None, None, None),
        ("Daily News", None, None, None, "News every hour")]
    # Every chat keeps its ID, and no ID was made up for a chat already known.
    assert dict(puid_map.user_names) == user_names
    assert [chat.puid for chat in session.chats()] == puids
    channel.logger.exception.assert_not_called()


def test_failed_refresh_is_only_logged_and_the_next_one_still_scheduled(channel):
    session = Session()
    session.core.get_contact.side_effect = RuntimeError("network unreachable")
    timer = use_session(channel, session)
    channel.done_reauth = Mock()

    channel._refresh_contacts(session)

    channel.logger.exception.assert_called_once()
    assert channel.sent_to_master == []
    assert channel.done_reauth.mock_calls == []
    assert channel._contact_refresh_timer is not timer
    assert is_pending(channel._contact_refresh_timer)


def test_next_refresh_is_scheduled_only_when_one_has_finished(channel):
    session = Session()
    timer = use_session(channel, session)
    timer_during_fetch = []
    session.core.get_contact.side_effect = \
        lambda update: timer_during_fetch.append(channel._contact_refresh_timer)

    channel._refresh_contacts(session)

    assert timer_during_fetch == [timer]
    next_timer = channel._contact_refresh_timer
    assert next_timer is not timer
    assert is_pending(next_timer)
    assert next_timer.daemon
    assert next_timer.interval == ONE_DAY


def test_scheduled_refresh_runs_on_its_own_and_repeats(channel):
    session = Session()
    fetches = threading.Semaphore(0)
    session.core.get_contact.side_effect = lambda update: fetches.release()
    channel.CONTACT_REFRESH_INTERVAL = 0.01

    use_session(channel, session)

    assert fetches.acquire(timeout=5)
    assert fetches.acquire(timeout=5)


def test_refresh_is_skipped_quietly_while_logged_out(channel):
    session = Session()
    session.alive = False
    timer = use_session(channel, session)

    channel._refresh_contacts(session)

    session.core.get_contact.assert_not_called()
    for level in ("exception", "error", "warning"):
        getattr(channel.logger, level).assert_not_called()
    assert channel.sent_to_master == []
    # It is tried again later, in case the session is back by then.
    assert channel._contact_refresh_timer is not timer
    assert is_pending(channel._contact_refresh_timer)


def test_refresh_of_a_replaced_session_does_nothing(channel):
    old_session, new_session = Session(), Session()
    use_session(channel, old_session)
    timer = use_session(channel, new_session)

    channel._refresh_contacts(old_session)

    old_session.core.get_contact.assert_not_called()
    assert channel._contact_refresh_timer is timer
    assert is_pending(timer)


def test_stopping_the_channel_cancels_the_refresh(channel):
    session = Session()
    timer = use_session(channel, session)
    still_scheduled = []
    session.cleanup.side_effect = \
        lambda: still_scheduled.append(is_pending(timer))

    channel.stop_polling()

    # Cancelled before the session itself is shut down.
    assert still_scheduled == [False]
    assert channel._contact_refresh_timer is None

    # A refresh that fires anyway does nothing and schedules nothing.
    channel._refresh_contacts(session)
    session.core.get_contact.assert_not_called()
    assert channel._contact_refresh_timer is None


def test_refresh_cancelled_while_running_is_not_scheduled_again(channel):
    session = Session()
    use_session(channel, session)
    session.core.get_contact.side_effect = \
        lambda update: channel._cancel_contact_refresh()

    channel._refresh_contacts(session)

    assert channel._contact_refresh_timer is None


def test_refresh_running_when_the_channel_starts_to_stop_is_not_scheduled_again(
        channel):
    session = Session()
    timer = use_session(channel, session)
    session.core.get_contact.side_effect = \
        lambda update: channel._stop_polling_event.set()

    channel._refresh_contacts(session)

    # Nothing new was scheduled in place of the timer of this very run.
    assert channel._contact_refresh_timer is timer


def test_first_login_schedules_a_refresh_one_day_later(channel, monkeypatch):
    session = Mock(name="first session")

    log_in(channel, monkeypatch, session, first_start=True)

    timer = channel._contact_refresh_timer
    assert is_pending(timer)
    assert timer.daemon
    assert timer.interval == ONE_DAY
    assert timer.function == channel._refresh_contacts
    assert timer.args == (session,)


def test_logging_in_again_moves_refresh_and_id_mapping_to_the_new_session(
        channel, monkeypatch):
    old_session, new_session = Mock(name="old session"), Mock(name="new session")
    old_timer = use_session(channel, old_session)
    events = []
    old_session.cleanup.side_effect = \
        lambda: events.append(("old session shut down", is_pending(old_timer)))
    old_session.puid_map.close.side_effect = \
        lambda: events.append("old mapping retired")
    new_session.enable_puid.side_effect = \
        lambda *args: events.append("new mapping loaded")

    log_in(channel, monkeypatch, new_session)

    # The old refresh is cancelled by the time the old session is shut down,
    # and the old mapping is retired before the new one reads the same file.
    assert events == [("old session shut down", False),
                      "old mapping retired", "new mapping loaded"]
    timer = channel._contact_refresh_timer
    assert timer is not old_timer
    assert is_pending(timer)
    assert timer.daemon
    assert timer.args == (new_session,)


def test_login_completes_when_the_previous_id_mapping_cannot_be_retired(
        channel, monkeypatch):
    old_session, new_session = Mock(name="old session"), Mock(name="new session")
    use_session(channel, old_session)
    old_session.puid_map.close.side_effect = OSError("disk full")

    log_in(channel, monkeypatch, new_session)

    assert channel.bot is new_session
    new_session.enable_puid.assert_called_once()
    channel.logger.exception.assert_called_once()
    assert [msg.text for msg in channel.sent_to_master] == \
        ["Switch complete, you can send and receive messages now."]


def test_failed_login_leaves_previous_id_mapping_in_charge(channel, monkeypatch):
    old_session = Mock(name="old session")
    old_timer = use_session(channel, old_session)

    def failing_login(**kwargs):
        raise RuntimeError("contact list unavailable")

    monkeypatch.setattr(efb_wechat_slave.wxpy, "Bot", failing_login)
    channel.authenticate("console_qr_code")

    # No other mapping has taken over the file, so the old one keeps saving.
    old_session.puid_map.close.assert_not_called()
    assert not is_pending(old_timer)
    assert channel._contact_refresh_timer is None
