# coding: utf-8
from __future__ import unicode_literals

import atexit
import collections
import difflib
import os
import pickle
import secrets
import shutil
import time
import logging
import logging.handlers

import threading
from typing import Optional, TYPE_CHECKING, Tuple
from collections import UserDict

if TYPE_CHECKING:
    from ..api.chats.chat import Chat

"""

# puid

尝试用聊天对象已知的属性，来查找对应的持久固定并且唯一的 用户 id


## 数据结构

PuidMap 中包含 4 个 dict，分别为

1. user_name -> puid
2. wxid -> puid
3. remark_name -> puid
4. caption (昵称, 性别, 省份, 城市, 签名) -> puid


## 查询逻辑

当给定一个 Chat 对象，需要获取对应的 puid 时，将按顺序，使用自己的对应属性，轮询以上 4 个 dict

* 若匹配任何一个，则获取到 puid，并将其他属性更新到其他的 dict
* 如果没有一个匹配，则创建一个新的 puid，并加入到以上的 4 个 dict


## 重新登录后的资料变动

每次登录后 user_name 都会改变，没有备注名的联系人只能依靠 caption 找回原有的 puid。
为了避免签名或地区的变动导致联系人被分配新的 puid，对于联系人列表中的聊天对象 (好友、公众号、群聊)，
在以上匹配均失败时，会尝试宽松匹配:

* 候选为昵称相同、性别不冲突，且在本次登录中尚未被任何聊天对象使用的旧 puid
* 性别、省份、城市、签名中，双方均有值但不相同的项目不得超过 2 项
* 按签名和地区的相似程度排序，仅当存在唯一的最佳候选时才沿用旧 puid，否则仍创建新的 puid
* 群成员不参与宽松匹配

登录后会先对整个联系人列表进行一次对账 (`PuidMap.reconcile`)，按依据从强到弱分步进行，
每一步处理完所有剩余的聊天对象后才进入下一步:

1. user_name、wxid、备注名
2. 与已保存的 caption 完全相同
3. 与已保存的 caption 不冲突 (允许缺少部分项目)
4. 宽松匹配

在前两步中被取用的 puid，不会在后续步骤中凭较弱的依据再分配给其他聊天对象；
后续步骤也不会凭较弱的依据把聊天对象匹配到会因此夺走其 caption 的 puid 上。
除此之外，匹配到同一条 caption 的多个聊天对象 (例如同名的群聊) 仍会像以往一样共用同一个 puid。

聊天对象在对账中得到的 puid 在本次登录中保持不变: 之后查询该聊天对象时直接返回这个 puid，
即使它与其他聊天对象共用该 puid；仅当它的属性与上一次保存时不同，才会重新记录。

对账顺利完成后，本次登录中稍后才出现的好友、公众号、群聊也可以使用宽松匹配，
但不会得到本次登录中已被其他聊天对象使用的 puid，除非是通过它自己的 user_name 或 wxid 找到的:
凭备注名或 caption 匹配到这样的 puid 时会跳过，没有其他匹配时创建新的 puid，
并且记录时不会夺走这些 puid 已保存的备注名和 caption。群成员等其他聊天对象的查询方式不变。

若对账没有进行或中途失败，本次登录中的所有查询都只使用精确匹配，方式与以往相同。


"""

# Type definitions
Caption = Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]

file_io_logger: logging.Logger = logging.getLogger(__name__)

# Attributes of a chat read once, so that matching does not touch the chat again.
_ChatInfo = collections.namedtuple('_ChatInfo', ('label', 'user_name', 'attrs', 'caption', 'relaxable'))


class _UsedByOthers:
    """The PUIDs used in a login session by chats other than the given one."""

    def __init__(self, users, user_name):
        self._users = users
        self._user_name = user_name

    def __contains__(self, puid):
        users = self._users.get(puid)
        return bool(users) and self._user_name not in users


class PuidMap(object):

    SYSTEM_ACCOUNTS = {
        'filehelper': '文件传输助手',
        'newsapp': '腾讯新闻',
        'fmessage': '朋友推荐消息',
        'weibo': '微博账号 (weibo)',
        'qqmail': 'QQ邮箱 (qqmail)',
        'tmessage': '锑字消息 (tmessage)',
        'qmessage': '秋字消息 (qmessage)',
        'qqsync': 'QQ同步 (qqsync)',
        'floatbottle': '漂流瓶',
        'lbsapp': '位置分享 (lbsapp)',
        'shakeapp': '摇一摇',
        'medianote': '语音记事本',
        'qqfriend': 'QQ好友 (qqfriend)',
        'readerapp': '阅读应用 (readerapp)',
        'blogapp': '博客应用 (blogapp)',
        'facebookapp': 'Facebook',
        'masssendapp': '群发应用 (masssendapp)',
        'meishiapp': '美食应用 (meishiapp)',
        'feedsapp': '订阅应用 (feedsapp)',
        'voip': '网络通话 (voip)',
        'blogappweixin': '微信博客 (blogappweixin)',
        'weixin': '微信团队',
        'brandsessionholder': '品牌会话 (brandsessionholder)',
        'weixinreminder': '微信提醒 (weixinreminder)',
        'officialaccounts': '官方账号 (officialaccounts)',
        'notification_messages': '通知消息 (notification_messages)',
        'wxitil': '微习提尔 (wxitil)',
        'userexperience_alarm': '用户体验 (userexperience_alarm)'
    }

    DUMP_TIMEOUT = 30
    """Number of seconds before auto dump upon lookups."""

    CLOSE_TIMEOUT = 5
    """
    Number of seconds retiring a map waits in all for a save or lookup in
    progress to finish. After that it is retired without waiting further.
    """

    PLACEHOLDER_NICK_NAMES = frozenset(("该账号已注销", "该账号已冻结", "账号已迁移"))
    """Nicknames shown for deleted, frozen or migrated accounts. Unrelated
    accounts share them, so they never identify a chat by common attributes."""

    MAX_CHANGED_ATTRIBUTES = 2
    """
    How many of gender, province, city and signature may have changed for a
    chat to still be recognised by its nick name.
    """

    logger: Optional[logging.Logger] = None

    def __init__(self, path, puid_logs=None):
        """
        用于获取聊天对象的 puid (持续有效，并且稳定唯一的用户ID)，和保存映射关系

        :param path: 映射数据的保存/载入路径
        :param puid_logs: PUID log path
        """
        self.path = path

        self.user_names = TwoWayDict()
        self.wxids = TwoWayDict()
        self.remark_names = TwoWayDict()

        self.captions = TwoWayDict()

        self._thread_lock = threading.Lock()
        # Held while the map is written to disk, so that saves never overlap.
        self._dump_lock = threading.Lock()
        # Per thread: user_names of the chats being read for a lookup. Reading
        # a chat may look up its own PUID again, e.g. for the name of a group.
        self._reading = threading.local()

        # PUIDs handed out by this instance, i.e. during the current login session,
        # each with the user_names of the chats it was given to. Kept in memory only.
        self._claimed = {}
        # nick_name -> PUIDs that have (or had) a caption with that nick_name.
        # Built on first use; entries are verified against ``captions`` when read.
        self._nick_index = None
        # Whether the contact list has been matched as a whole in this session.
        # Until then, PUIDs in use cannot be told from abandoned ones.
        self._reconciled = False
        # user_name -> the PUID a chat was given when the contact list was matched
        # in this session, with the attributes it was last saved with. Kept in
        # memory only.
        self._assigned = {}

        if puid_logs:
            self.logger = logging.getLogger(__name__)
            try:
                self.logger.addHandler(logging.handlers.RotatingFileHandler(puid_logs))
            except IOError:
                self.logger = None
        else:
            self.logger = None

        if os.path.exists(self.path):
            self.load()

        self._dump_task: Optional[threading.Timer] = None
        self._closed = False

        atexit.register(self.dump)

    def log(self, *args, **kwargs):
        if self.logger:
            self.logger.debug(*args, **kwargs)

    @property
    def attr_dicts(self):
        return self.user_names, self.wxids, self.remark_names

    def __len__(self):
        return len(self.user_names)

    def __bool__(self):
        return bool(self.path)

    def __nonzero__(self):
        return bool(self.path)

    def get_puid(self, chat: 'Chat'):
        """
        获取指定聊天对象的 puid

        :param chat: 指定的聊天对象
        :return: puid
        :rtype: str
        """

        if chat.user_name in self._chats_being_read():
            # Reading the chat needs its own PUID: it cannot be identified yet.
            return

        # The chat is read before taking the lock: reading it may need a PUID.
        info = self._snapshot(chat)

        with self._thread_lock:
            self.log("Querying chat for PUID: %s", info.label)

            if info.user_name in PuidMap.SYSTEM_ACCOUNTS:
                self.log("%s is a recognised system chat.", info.user_name)
                return info.user_name

            if not self._is_identifiable(info):
                self.log("%s has no user_name or nick_name.", info.label)
                return

            # A chat of the contact list keeps the PUID it was given there, also
            # when another chat has been recorded under that PUID since. It is
            # recorded again only if its attributes differ from those it was last
            # saved with.
            assigned = self._assigned.get(info.user_name)
            if assigned and assigned[1:] == (info.attrs, info.caption):
                self.log("Chat %s keeps PUID %s from the matching of the contact list.", info.label, assigned[0])
                return assigned[0]

            # Once the contact list has been matched, the PUIDs in use are known.
            # A contact found after that is not given one that another chat uses,
            # other than by its own user_name or wxid, and takes nothing away
            # from such a PUID when it is recorded.
            in_use = ()
            if self._reconciled and info.relaxable and not assigned:
                in_use = _UsedByOthers(self._claimed, info.user_name)

            puid = assigned[0] if assigned else (self._match_stable(info, in_use) or self._match_caption(info, in_use))
            if not puid and info.relaxable:
                if self._reconciled:
                    candidates = self._relaxed_candidates(info, in_use)
                    puid = self._unique_best(candidates)
                    if puid:
                        self._log_relaxed_match(info, puid)
                    elif candidates:
                        self.log("No single best PUID to reuse for %s among %s.", info.label, sorted(candidates))
                else:
                    self.log("%s can only be matched exactly, as the contact list has not been matched "
                             "in this session.", info.label)

            saved = self._saved_for(puid) if assigned else None
            puid, value_updated = self._store(info, puid, in_use)
            # The attributes are remembered whenever recording them changed what
            # is saved, so that a change that is undone later is recorded as well.
            # A contact reads differently when it shows up in a group; as long as
            # that changes nothing, its next lookup as a contact records nothing.
            if assigned and saved != self._saved_for(puid):
                self._assigned[info.user_name] = (puid, info.attrs, info.caption)

            if value_updated:
                self.activate_dump()
                self.log("Refreshing dump delay as value is changed.")

            return puid

    def reconcile(self, chats):
        """
        Assign PUIDs to a whole contact list at once, usually right after login.

        Chats are identified in steps, from the strongest evidence to the
        weakest: stable attributes, a stored caption that is the same as the
        current one, a stored caption with no conflicting value, and finally a
        stored caption with the same nick name of which a few values changed.
        Each step is applied to all remaining chats before the next one
        starts.

        A PUID taken in the first two steps is not given to another chat on
        the weaker evidence of the later ones, nor is a chat matched on such
        evidence to a PUID that would take over its caption. Apart from that,
        chats that match the same stored caption share its PUID, as they do
        when looked up one by one.

        In the last of these steps only PUIDs nobody has taken in this session
        are considered, and one is reused only when the chat and the PUID are
        each other's single best match.

        A chat keeps the PUID it is given here for the rest of the session: a
        later lookup returns it, and records the chat again only if its
        attributes differ from those it was last saved with.

        Chats that show up later in the session may be recognised by their
        nick name only once this has completed. From then on, a contact list
        entry that shows up later is not given a PUID that another chat uses
        in this session, unless its own user_name or wxid leads to it.

        :param chats: contact list entries (friends, official accounts, groups)
        """

        # Chats are read before taking the lock: reading one may need a PUID itself.
        infos = []
        seen = set()
        for chat in chats:
            try:
                info = self._snapshot(chat)
            except Exception:
                file_io_logger.debug("Skipping a chat that cannot be read.", exc_info=True)
                continue
            if (info.user_name in PuidMap.SYSTEM_ACCOUNTS or not self._is_identifiable(info)
                    or info.user_name in seen):
                continue
            seen.add(info.user_name)
            infos.append(info)

        with self._thread_lock:
            self.log("Reconciling %s chats.", len(infos))
            value_updated = False
            assigned = {}

            def store(info, puid):
                puid, updated = self._store(info, puid)
                assigned[info.user_name] = (puid, info.attrs, info.caption)
                return updated

            # 1. Chats identified by a stable attribute, then those with the very
            #    same caption as a stored one, take their PUIDs.
            taken = set()
            pending = infos
            for match in (self._match_stable, self._match_same_caption):
                unmatched = []
                for info in pending:
                    puid = match(info)
                    if puid:
                        value_updated |= store(info, puid)
                        taken.add(puid)
                    else:
                        unmatched.append(info)
                pending = unmatched

            # 2. Chats that merely do not conflict with a stored caption. On this
            #    and on weaker evidence, a chat is given neither a PUID taken
            #    above, nor one that would take over the caption of such a PUID.
            unmatched = []
            for info in pending:
                puid = self._match_caption_indexed(info, taken)
                if puid:
                    value_updated |= store(info, puid)
                else:
                    unmatched.append(info)
            pending = unmatched

            # 3. Pair the remaining chats with the PUIDs that are still free.
            #    All pairs are decided on the same state before any is applied.
            chat_scores = {}
            puid_scores = {}
            for i, info in enumerate(pending):
                if info.relaxable:
                    chat_scores[i] = self._relaxed_candidates(info, taken)
                    for puid, score in chat_scores[i].items():
                        puid_scores.setdefault(puid, {})[i] = score

            relaxed = {}
            for i, scores in chat_scores.items():
                puid = self._unique_best(scores)
                if puid and self._unique_best(puid_scores[puid]) == i:
                    relaxed[i] = puid
                elif scores:
                    self.log("No single best PUID to reuse for %s among %s.", pending[i].label, sorted(scores))

            for i, puid in relaxed.items():
                self._log_relaxed_match(pending[i], puid)
                value_updated |= store(pending[i], puid)

            # 4. Whatever is left gets a new PUID, unless it has an exact match by now.
            for i, info in enumerate(pending):
                if i not in relaxed:
                    puid = self._match_stable(info) or self._match_caption_indexed(info, taken)
                    value_updated |= store(info, puid)

            self._assigned.update(assigned)
            self._reconciled = True
            self.log("Contact list is matched. Chats found later may be matched by nick name.")

            if value_updated:
                self.activate_dump()
                self.log("Refreshing dump delay as value is changed.")

    def _chats_being_read(self) -> set:
        """The user_names of the chats being read in the current thread."""
        names = getattr(self._reading, 'names', None)
        if names is None:
            names = self._reading.names = set()
        return names

    def _snapshot(self, chat: 'Chat') -> _ChatInfo:
        """
        Read the attributes used for matching from a chat. Meanwhile, a
        lookup of the same chat in this thread finds no PUID.
        """
        user_name = chat.user_name
        reading = self._chats_being_read()
        reading.add(user_name)
        try:
            label = str(chat) if self.logger else None

            if user_name in PuidMap.SYSTEM_ACCOUNTS:
                return _ChatInfo(label, user_name, None, None, False)

            # 5 common attributes for matching:
            # 1. Chat base name
            # 2. Gender (for user)
            # 3. Province (for user)
            # 4. City (for user)
            # 5. Signature (for user)
            caption = self.get_caption(chat)

            # 3 of the stable attributes:
            # 1. Web WeChat temporary ID
            # 2. wxid (true permanent ID, almost unavailable)
            # 3. Remark name defined by user (Does this work?)
            attrs = (
                user_name,
                chat.wxid,
                getattr(chat, 'remark_name', None),
            )

            return _ChatInfo(label, user_name, attrs, caption, self._is_contact(chat))
        finally:
            reading.discard(user_name)

    @staticmethod
    def _is_contact(chat: 'Chat') -> bool:
        """Whether the chat is a contact list entry, as opposed to e.g. a group member."""
        from ..api.chats import MP, Friend, Group
        return isinstance(chat, (Friend, Group, MP))

    @staticmethod
    def _is_identifiable(info: _ChatInfo) -> bool:
        return bool(info.user_name and info.caption and info.caption[0])

    def _match_stable(self, info: _ChatInfo, exclude=()):
        """
        Look up the PUID of a chat by its stable attributes. A remark name
        does not lead to a PUID in `exclude`. The thread lock must be held.

        :return: the PUID, or `None` if there is no match
        """
        self.log("Trying to match stable attributes: %s", info.attrs)
        for i in range(3):
            puid = self.attr_dicts[i].get(info.attrs[i])
            if puid and i == 2 and puid in exclude:
                self.log("Chat %s is not matched to PUID %s with attribute %s, as another chat uses it.",
                         info.label, puid, i)
            elif puid:
                self.log("Chat %s is matched to PUID %s with attribute %s", info.label, puid, i)
                return puid

    def _match_caption(self, info: _ChatInfo, exclude=()):
        """
        Look up the PUID of a chat by the first stored caption with no
        conflicting value. The PUIDs in `exclude` are passed over, and so is a
        caption that would take over the caption of one of them once the chat
        is recorded. The thread lock must be held.

        :return: the PUID, or `None` if there is no match
        """
        self.log("Stable attribute failed, trying to match common attributes: %s", info.caption)
        for caption in self.captions:
            if self.match_captions(caption, info.caption):
                if self.captions[caption] in exclude or self._takes_caption(caption, info.caption, exclude):
                    self.log("Chat %s is not matched to PUID %s attributes %s, as that would take a PUID "
                             "or its attributes from another chat.", info.label, self.captions[caption], caption)
                    continue
                return self._matched_by_caption(info, caption)

    def _match_caption_indexed(self, info: _ChatInfo, exclude=()):
        """
        Same as `_match_caption`, but only looks at captions with the same
        nick_name, and leaves the PUIDs in `exclude` and their captions alone.
        """
        self.log("Stable attribute failed, trying to match common attributes: %s", info.caption)
        return self._matched_by_caption(info, self._find_caption_indexed(info.caption, self.match_captions, exclude))

    def _match_same_caption(self, info: _ChatInfo):
        """
        Look up the PUID of a chat by a stored caption with the very same
        values. The thread lock must be held.

        :return: the PUID, or `None` if there is no match
        """
        return self._matched_by_caption(info, self._find_caption_indexed(info.caption, self.same_captions))

    def _matched_by_caption(self, info: _ChatInfo, caption):
        if caption:
            puid = self.captions[caption]
            self.log("Chat %s is matched to PUID %s attributes %s", info.label, puid, caption)
            return puid

    def _find_caption_indexed(self, chat_caption: Caption, match, exclude=()):
        """
        Find the stored caption with the same nick_name as the caption of a
        chat for which `match(stored, chat_caption)` holds. If there are
        several, the one stored first is returned.

        The captions of the PUIDs in `exclude` are left out, and so is a
        caption that would take over the caption of one of them once the chat
        is recorded.
        """
        matched = set()
        for puid in self._nick_puids(chat_caption[0]):
            if puid in exclude:
                continue
            caption = self.captions.get_key(puid)
            if caption and match(caption, chat_caption) and not self._takes_caption(caption, chat_caption, exclude):
                matched.add(caption)

        if len(matched) > 1:
            for caption in self.captions:
                if caption in matched:
                    return caption
        elif matched:
            return matched.pop()

    def _takes_caption(self, caption: Caption, chat_caption: Caption, puids) -> bool:
        """
        Whether recording a chat under the PUID of a stored caption would take
        the resulting caption away from one of the given PUIDs.
        """
        return self.captions.get(self.merge_captions(caption, chat_caption)) in puids

    def _nick_puids(self, nick_name):
        """
        PUIDs that may have a caption with the given nick_name. Callers must
        check the current caption of each, as outdated entries are not removed.
        """
        if self._nick_index is None:
            self._nick_index = {}
            for caption, puid in self.captions.items():
                self._nick_index.setdefault(caption[0], set()).add(puid)
        return self._nick_index.get(nick_name, ())

    def _relaxed_candidates(self, info: _ChatInfo, keep=()):
        """
        PUIDs a chat may reuse although some of its common attributes changed:
        same nick_name, no conflicting gender, no more changed attributes than
        `MAX_CHANGED_ATTRIBUTES`, and not used by any chat in this session.
        One that would take over the caption of a PUID in `keep` once the chat
        is recorded is left out. The thread lock must be held.

        :return: dict of PUID -> similarity to the chat
        """
        nick_name = info.caption[0]
        candidates = {}
        if nick_name in self.PLACEHOLDER_NICK_NAMES:
            return candidates
        for puid in self._nick_puids(nick_name):
            if puid in self._claimed:
                continue
            caption = self.captions.get_key(puid)
            if not caption or caption[0] != nick_name:
                continue
            if caption[1] and info.caption[1] and caption[1] != info.caption[1]:
                continue
            changed = sum(1 for i in range(1, 5)
                          if caption[i] and info.caption[i] and caption[i] != info.caption[i])
            if changed > self.MAX_CHANGED_ATTRIBUTES:
                continue
            if self._takes_caption(caption, info.caption, keep):
                continue
            candidates[puid] = self._similarity(caption, info.caption)
        return candidates

    @staticmethod
    def _similarity(old: Caption, new: Caption) -> float:
        """
        Similarity of two captions with the same nick_name: number of other
        attributes that are equal, plus how alike the signatures are (0 to 1).
        """
        score = 0.0
        for i in range(1, 5):
            if old[i] and new[i] and old[i] == new[i]:
                score += 1
        if old[4] and new[4]:
            score += difflib.SequenceMatcher(None, old[4], new[4]).ratio()
        return score

    @staticmethod
    def _unique_best(scores):
        """The key with the highest score, or `None` if there is none or it is shared."""
        best = None
        best_score = None
        tied = False
        for key, score in scores.items():
            if best_score is None or score > best_score:
                best, best_score, tied = key, score, False
            elif score == best_score:
                tied = True
        return None if tied else best

    def _log_relaxed_match(self, info: _ChatInfo, puid):
        self.log("Chat %s is matched to PUID %s by nick name, as its common attributes changed from %s to %s",
                 info.label, puid, self.captions.get_key(puid), info.caption)

    def _leads_contact_away(self, caption: Caption, puids) -> bool:
        """
        Whether a caption, once stored for another PUID, would lead a contact
        away from one of the given PUIDs after the next login: the contact was
        given that PUID when the contact list was matched and shows the very
        same caption, while the caption stored for its PUID is not the very
        same and its remark name, if any, is not stored for it.
        """
        if not puids:
            return False
        for puid in self._nick_puids(caption[0]):
            stored = self.captions.get_key(puid)
            if puid in puids and stored and not self.same_captions(stored, caption):
                for user_name in self._claimed[puid]:
                    assigned = self._assigned.get(user_name)
                    if (assigned and self.same_captions(assigned[2], caption)
                            and self.remark_names.get(assigned[1][2]) != puid):
                        return True
        return False

    def _saved_for(self, puid):
        """What is saved for a PUID, apart from the user_name of the chat recorded last."""
        return [attr_dict.get_key(puid) for attr_dict in (self.wxids, self.remark_names, self.captions)]

    def _store(self, info: _ChatInfo, puid, keep=()):
        """
        Record a chat under a PUID, or under a new PUID if `None` is given,
        and mark the PUID as used by the chat in this session. A remark name
        or caption stored for a PUID in `keep` is left with that PUID, and a
        caption that would lead a contact away from such a PUID is not
        stored; without the caption, only the user_name of the chat is
        stored. The thread lock must be held.

        :return: the PUID, and whether anything worth saving has changed
        """
        old_caption = self.captions.get_key(puid) if puid else None
        new_caption = self.merge_captions(old_caption, info.caption)

        # The caption of a PUID in `keep` stays with it, and no caption is
        # stored that fits a contact using such a PUID better than the one
        # stored for that PUID. Then only the user_name is stored: found by
        # another attribute on the next login, this PUID would take the
        # caption, or the contact, after all.
        caption_kept = self.captions.get(new_caption) in keep or self._leads_contact_away(new_caption, keep)

        if puid:
            value_updated = old_caption != new_caption and not caption_kept
            if old_caption is None and new_caption in self.captions:
                # Chats with identical common attributes take turns holding
                # the single entry for them: nothing new to save.
                value_updated = False
            if value_updated:
                self.log("Updating common attributes of %s from %s to %s", puid, old_caption, new_caption)
        else:
            puid = info.user_name[-8:]
            self.log("Discovered new chat %s with common attributes %s. Assign new PUID: %s",
                     info.label, new_caption, puid)
            value_updated = True

        for i in range(3):
            chat_attr = info.attrs[i]
            if chat_attr and self.attr_dicts[i].get(chat_attr) not in keep and not (i and caption_kept):
                old_attr = self.attr_dicts[i].get_key(puid)
                if old_attr != chat_attr:
                    value_updated = True
                    self.log("Updating stable attributes #%s of PUID %s from %s to %s",
                             i, puid, old_attr, chat_attr)
                self.attr_dicts[i][chat_attr] = puid

        if not caption_kept:
            self.captions[new_caption] = puid
            if self._nick_index is not None:
                self._nick_index.setdefault(new_caption[0], set()).add(puid)

        self._claimed.setdefault(puid, set()).add(info.user_name)

        return puid, value_updated

    def activate_dump(self):
        """Activate dump timeout"""
        if self._closed:
            return

        if self._dump_task:
            self._dump_task.cancel()

        self._dump_task = threading.Timer(self.DUMP_TIMEOUT, self.dump)
        self._dump_task.start()

    def dump(self):
        """
        保存映射数据
        """
        self._dump()

    def _dump(self, timeout=-1) -> bool:
        """
        Save the map, unless it is retired. Saves never overlap: this waits
        for a save in progress, at most `timeout` seconds if not negative.

        :return: `False` if the wait timed out and nothing was done
        """
        if not self._dump_lock.acquire(timeout=timeout):
            return False
        try:
            if self._closed:
                return True

            # What a pending automatic dump would save is saved now.
            dump_task, self._dump_task = self._dump_task, None
            if dump_task:
                dump_task.cancel()

            # Safe dump: the map file exists at every moment once written.
            data = (self.user_names, self.wxids, self.remark_names, self.captions)
            temp_path = f"{self.path}.{secrets.token_urlsafe(8)}"
            bak_path = f"{self.path}.bak"

            file_io_logger.debug(f"Write PUID mapping to {temp_path}.")
            with open(temp_path, "wb") as f:
                pickle.dump(data, f)
                f.flush()
                os.fsync(f.fileno())

            if os.path.exists(self.path):
                file_io_logger.debug(f"Backup old PUID mapping to {bak_path}")
                bak_temp_path = f"{bak_path}.{secrets.token_urlsafe(8)}"
                shutil.copyfile(self.path, bak_temp_path)
                os.replace(bak_temp_path, bak_path)

            if self._closed:
                # Retired while saving: another map may own the file by now.
                os.unlink(temp_path)
                return True

            file_io_logger.debug(f"Move new PUID mapping from {temp_path} to {self.path}")
            os.replace(temp_path, self.path)
            file_io_logger.debug(f"PUID mapping overwrite completed.")

            self.log("Successfully dumped PUID map to: %s", self.path)
            self.log("Dumped - user_names: %s", self.user_names)
            self.log("Dumped - wxids: %s", self.wxids)
            self.log("Dumped - remark_names: %s", self.remark_names)
            self.log("Dumped - captions: %s", self.captions)
            return True
        finally:
            self._dump_lock.release()

    def close(self):
        """
        Retire this map, for example when another map takes over its file.

        Changes still waiting for the next automatic dump are written once.
        After that the map never writes again, neither from a pending timer
        nor at exit. Lookups in memory keep working.

        Waits at most `CLOSE_TIMEOUT` seconds in all for a save or lookup in
        progress; the map is retired all the same once that time is up.
        """
        deadline = time.monotonic() + self.CLOSE_TIMEOUT
        try:
            # A save already running may be the one that holds the latest changes.
            pending = self._dump_task or self._dump_lock.locked()
            if pending and not self._dump(self.CLOSE_TIMEOUT):
                file_io_logger.warning(
                    "Timed out waiting for a save of the PUID mapping in progress; "
                    "retiring it without saving its latest changes.")
        finally:
            locked = self._thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
            try:
                self._closed = True
                # A lookup may have scheduled another dump in the meantime.
                dump_task, self._dump_task = self._dump_task, None
            finally:
                if locked:
                    self._thread_lock.release()
            if dump_task:
                dump_task.cancel()
            atexit.unregister(self.dump)
            if not locked:
                file_io_logger.warning(
                    "Timed out waiting for a PUID lookup in progress; "
                    "the PUID mapping is retired all the same.")

    def load(self, recur=False):
        """
        载入映射数据
        """
        self.log("Loading PUID map from local disk: %s", self.path)
        try:
            with open(self.path, 'rb') as fp:
                self.user_names, self.wxids, self.remark_names, self.captions = pickle.load(fp)
                self._nick_index = None
                self.log("Local disk - user_names: %s", self.user_names)
                self.log("Local disk - wxids: %s", self.wxids)
                self.log("Local disk - remark_names: %s", self.remark_names)
                self.log("Local disk - captions: %s", self.captions)
                # Migrate old 4-tuple captions to 5-tuple (add signature as None)
                self._migrate_captions()
        except (ImportError, ModuleNotFoundError) as e:
            # Mitigate the pickling issue of migrating .wxpy to .vendor.wxpy
            if recur:
                raise e
            src = open(self.path, 'rb').read()
            src = src.replace(b'cefb_wechat_slave.wxpy.', b'cefb_wechat_slave.vendor.wxpy.')
            with open(self.path, 'wb') as f:
                f.write(src)
            return self.load(recur=True)

    def _migrate_captions(self):
        """Migrate old 4-tuple captions to 5-tuple format (add signature as None)."""
        migrated = TwoWayDict()
        needs_migration = False
        for caption, puid in self.captions.items():
            if len(caption) == 4:
                # Old format: (nick_name, sex, province, city)
                # New format: (nick_name, sex, province, city, signature)
                caption = (*caption, None)
                needs_migration = True
            migrated[caption] = puid
        if needs_migration:
            self.captions = migrated
            self._nick_index = None
            self.log("Migrated captions from 4-tuple to 5-tuple format")

    @staticmethod
    def get_caption(chat: 'Chat') -> Caption:
        return (
            chat.nick_name,
            getattr(chat, 'sex', None),
            getattr(chat, 'province', None),
            getattr(chat, 'city', None),
            getattr(chat, 'signature', None),
        )

    def match_captions(self, old: Caption, new: Caption) -> bool:
        """Full match of a 5-value tuple only when both side has a value"""
        if new[0] in self.PLACEHOLDER_NICK_NAMES:
            return False
        if new[0] and old:
            for i in range(5):
                if old[i] and new[i] and old[i] != new[i]:
                    if i > 0:
                        self.log("Potential common attribute match: %s -> %s", old, new)
                    return False
            return True
        return False

    def same_captions(self, old: Caption, new: Caption) -> bool:
        """Match of a 5-value tuple only when both sides have the same values, or none, everywhere"""
        if new[0] in self.PLACEHOLDER_NICK_NAMES:
            return False
        return all((old[i] or None) == (new[i] or None) for i in range(5))

    @staticmethod
    def merge_captions(old: Optional[Caption], new: Caption) -> Caption:
        """Merge a 5-value tuple where new values replaces old values"""
        if not old:
            return new
        else:
            cap: Caption = (new[0] or old[0], new[1] or old[1], new[2] or old[2], new[3] or old[3], new[4] or old[4])
            return cap


class TwoWayDict(UserDict):
    """
    可双向查询，且 key, value 均为唯一的 dict
    限制: key, value 均须为不可变对象，且不支持 .update() 方法
    """

    def __init__(self):
        super(TwoWayDict, self).__init__()
        self._reversed = dict()

    def get_key(self, value):
        """
        通过 value 查找 key
        """
        return self._reversed.get(value)

    def del_value(self, value):
        """
        删除 value 及对应的 key
        """
        del self[self._reversed[value]]

    def __setitem__(self, key, value):
        if self.get(key) != value:
            if key in self:
                self.del_value(self[key])
            if value in self._reversed:
                del self[self.get_key(value)]
            self._reversed[value] = key
            return super(TwoWayDict, self).__setitem__(key, value)

    def __delitem__(self, key):
        del self._reversed[self[key]]
        return super(TwoWayDict, self).__delitem__(key)

    def update(*args, **kwargs):
        raise NotImplementedError
