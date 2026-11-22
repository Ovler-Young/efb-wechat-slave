import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot import MsgType, coordinator
from ehforwarderbot.chat import SelfChatMember
from ehforwarderbot.exceptions import EFBMessageError
from ehforwarderbot.status import MessageRemoval

import efb_wechat_slave
from efb_wechat_slave import WeChatChannel

LOADING_TEXT = "Scanned successfully, loading contacts…"
COMPLETE_TEXT = "Switch complete, you can send and receive messages now."
RELOGIN_TEXT = "WeChat is re-logging in, please retry later."


def make_bot(name):
    """A stand-in for a logged-in session with a single chat."""
    chat = Mock(name=f"{name} chat")
    chat.send_msg.return_value = SimpleNamespace(
        chat=SimpleNamespace(puid="puid"), id=f"{name} id", local_id=f"{name} local id")
    return Mock(name=name, chat=chat)


def make_channel(monkeypatch, tmp_path, bot_factory):
    """
    Build a channel logged in with an "old" session, where logging in again
    obtains its session from ``bot_factory`` and messages sent to the master
    channel are collected in ``channel.sent_to_master``.
    """
    channel = WeChatChannel.__new__(WeChatChannel)
    channel._ = lambda text: text
    channel.logger = Mock()
    channel.flag = Mock(return_value=None)
    channel.qr_uuid = ("", 0)
    channel.done_reauth = threading.Event()
    channel.done_reauth.set()
    channel.user_auth_chat = SimpleNamespace(other=object())
    channel.bot = make_bot("old")
    channel.slave_message = Mock()
    # Chats are always looked up in the session the channel currently holds.
    channel.chats = SimpleNamespace(
        get_wxpy_chat_by_uid=lambda uid: channel.bot.chat)
    channel.sent_to_master = []

    monkeypatch.setattr(efb_wechat_slave.wxpy, "Bot", bot_factory)
    monkeypatch.setattr(efb_wechat_slave.efb_utils,
                        "get_data_path", lambda channel_id: tmp_path)
    monkeypatch.setattr(coordinator, "master", object(), raising=False)
    monkeypatch.setattr(coordinator, "send_message",
                        channel.sent_to_master.append)
    return channel


def text_message():
    return SimpleNamespace(chat=SimpleNamespace(uid="puid"), uid="outgoing",
                           type=MsgType.Text, text="hello", edit=False, target=None)


def test_message_sent_during_relogin_waits_for_the_new_session(monkeypatch, tmp_path):
    new_bot = make_bot("new")
    loading = threading.Event()
    loaded = threading.Event()

    def bot_factory(**kwargs):
        loading.set()
        assert loaded.wait(5)
        return new_bot

    channel = make_channel(monkeypatch, tmp_path, bot_factory)
    old_bot = channel.bot

    relogin = threading.Thread(
        target=channel.authenticate, args=("master_qr_code",))
    relogin.start()
    assert loading.wait(5)

    result = []
    sender = threading.Thread(
        target=lambda: result.append(channel.send_message(text_message())))
    sender.start()
    sender.join(0.2)
    assert sender.is_alive()
    old_bot.chat.send_msg.assert_not_called()

    loaded.set()
    sender.join(5)
    relogin.join(5)
    assert not sender.is_alive()
    old_bot.chat.send_msg.assert_not_called()
    new_bot.chat.send_msg.assert_called_once_with("hello")
    assert json.loads(result[0].uid) == [["puid", "new id", "new local id"]]


def test_message_fails_with_clear_error_when_relogin_takes_too_long(monkeypatch, tmp_path):
    channel = make_channel(monkeypatch, tmp_path, bot_factory=None)
    channel.REAUTH_WAIT_TIMEOUT = 0.05
    channel.chats = Mock()
    channel.done_reauth.clear()

    with pytest.raises(EFBMessageError) as send_error:
        channel.send_message(text_message())
    assert str(send_error.value) == RELOGIN_TEXT

    recall = MessageRemoval.__new__(MessageRemoval)
    recall.message = SimpleNamespace(
        author=SelfChatMember.__new__(SelfChatMember),
        uid=json.dumps([["puid", "id", "local id"]]))
    with pytest.raises(EFBMessageError) as recall_error:
        channel.send_status(recall)
    assert str(recall_error.value) == RELOGIN_TEXT

    # Nothing was looked up in, or sent through, the session being replaced.
    channel.chats.get_wxpy_chat_by_uid.assert_not_called()


def test_relogin_notifies_master_when_scanned_and_when_ready(monkeypatch, tmp_path):
    new_bot = make_bot("new")

    def bot_factory(qr_callback, **kwargs):
        qr_callback("uuid", "200")
        return new_bot

    channel = make_channel(monkeypatch, tmp_path, bot_factory)
    notices = []

    def record(msg):
        notices.append((msg.text, channel.bot is new_bot,
                        channel.slave_message.wechat_msg_register.called))

    monkeypatch.setattr(coordinator, "send_message", record)

    channel.authenticate("master_qr_code")

    # (text, new session in use, message handlers registered)
    assert notices == [(LOADING_TEXT, False, False),
                       (COMPLETE_TEXT, True, True)]
    assert channel.slave_message.bot is new_bot
    assert channel.done_reauth.is_set()


def test_failed_relogin_is_reported_to_master_and_releases_messages(monkeypatch, tmp_path):
    def bot_factory(**kwargs):
        raise RuntimeError("contact list unavailable")

    channel = make_channel(monkeypatch, tmp_path, bot_factory)

    channel.authenticate("master_qr_code")

    assert [msg.text for msg in channel.sent_to_master] == [
        "Failed to log in again: contact list unavailable"]
    assert channel.sent_to_master[0].deliver_to is coordinator.master
    assert channel.done_reauth.is_set()


def test_first_login_neither_holds_messages_nor_notifies_master(monkeypatch, tmp_path):
    held_during_login = []
    outcomes = [make_bot("first"), RuntimeError("login failed")]

    def bot_factory(**kwargs):
        held_during_login.append(not channel.done_reauth.is_set())
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    channel = make_channel(monkeypatch, tmp_path, bot_factory)

    channel.authenticate("console_qr_code", first_start=True)
    with pytest.raises(RuntimeError):
        channel.authenticate("console_qr_code", first_start=True)

    assert held_during_login == [False, False]
    assert channel.sent_to_master == []
