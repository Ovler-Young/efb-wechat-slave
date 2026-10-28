from xml.etree import ElementTree as ETree

from efb_wechat_slave.slave_message import SlaveMessageManager


def test_slave_message_get_node_text():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <root>
        <item>
            <child>Text</child>
        </item>
        <emptyItem/>
    </root>
    """

    root = ETree.fromstring(xml)

    assert SlaveMessageManager.get_node_text(root, "./item/child", "") == "Text"
    assert SlaveMessageManager.get_node_text(root, "./item/non_existing", "fallback") == "fallback"
    assert SlaveMessageManager.get_node_text(root, "./emptyItem", "fallback") == "fallback"


def test_sharing_chat_record_preview():
    from types import SimpleNamespace
    from unittest.mock import Mock

    manager = SlaveMessageManager.__new__(SlaveMessageManager)
    manager.logger = Mock()
    manager._ = lambda text: text
    manager.wechat_text_msg = lambda msg: msg.raw['text']
    msg = SimpleNamespace(id='record', articles=None, raw={'Content': """<msg><appmsg>
        <type>40</type><realinnertype>19</realinnertype>
        <title>群聊的聊天记录</title><des>甲: hello
乙: [图片]</des><url>https://weixin.qq.com/upgrade</url>
        <xmlfulllen>86732</xmlfulllen>
    </appmsg></msg>"""})

    assert manager.wechat_sharing_msg(msg) == (
        '群聊的聊天记录\n\n甲: hello\n乙: [图片]\n\n'
        '[Chat record preview. Open WeChat to view the full record.]'
    )
