"""Small, deterministic evidence normalizations (part of the scoring fingerprint)."""

from __future__ import annotations

from collections import Counter
import json
import re
import unicodedata
from decimal import Decimal, localcontext

_MONEY = re.compile(r"(?<![\d.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(万亿元|亿元|万元)")
_YUAN_AMOUNT = re.compile(r"(?<![\d.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*元")
_MULTIPLIERS = {'万亿元': Decimal('1000000000000'), '亿元': Decimal('100000000'), '万元': Decimal('10000')}


def normalize_money_units(text: str) -> str:
    """Allow exact decimal RMB unit conversions without changing claim context."""
    text = unicodedata.normalize('NFKC', text).replace('−', '-')

    def convert(match):
        digits = match[1].replace(',', '')
        with localcontext() as context:
            context.prec = max(28, len(digits) + 13)
            amount = Decimal(digits) * _MULTIPLIERS[match[2]]
        return format(amount, 'f') + '元'

    return _MONEY.sub(convert, text)


def _event_money_comparison_text(text: str) -> str:
    """Normalize exact RMB amounts only; keep currency and object context.

    Event object relevance is checked before numeric grounding. Equivalent
    amounts must reach that later guard instead of failing on their spelling.
    Do not normalize bare numbers (codes, years, quantities), remove currency
    labels, or round using the caller's Decimal precision.
    """
    def canonicalize(match):
        amount = format(Decimal(match[1].replace(',', '')), 'f')
        if '.' in amount:
            amount = amount.rstrip('0').rstrip('.')
        return amount + '元'

    return _YUAN_AMOUNT.sub(canonicalize, normalize_money_units(text))


_EVENT_BASE = (
    "签订", "中标", "获批", "收购", "量产", "投产", "建设",
    "减持", "增持", "获得", "支付", "投资",
)
# Surface forms that mean the same event verb. Extraction canonicalizes them so a
# source that writes 签署 still grounds a reason that writes 签订.
_EVENT_VERB_ALIASES = {
    "签订": ("签署",),
    "获得": ("取得",),
    "收购": ("并购",),
}
_EVENT_VERB_CANONICAL = {
    alias: canonical
    for canonical, aliases in _EVENT_VERB_ALIASES.items()
    for alias in aliases
}
_EVENT = re.compile(
    "|".join(
        _EVENT_BASE
        + tuple(alias for aliases in _EVENT_VERB_ALIASES.values() for alias in aliases)
    )
)
# Only these verbs are rejected when the source never states them. Broader
# verbs such as 投资 also occur in non-event commentary ("投资催化").
_SOURCE_REQUIRED_EVENTS = frozenset({"签订", "中标", "获批", "收购", "获得"})
_STATE = re.compile(
    r"(尚未|未曾|没有|否认|并未|不会|未|或将|预计|有望|可能|拟|计划|将|已经|已|正在)$"
)
_FORECAST_PREFIX = re.compile(r"^(预计|有望|可能|或将)")
_INFERENCE = re.compile(r'有望|可能|预计|利好|推动|支撑|有利于|促进|若|或将')
_GENERIC_SUBJECTS = {'公司', '该公司', '企业', '该企业', '集团', '该集团'}
_LITERAL_ORDER = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{1,40}公司)(?:的)?订单(?:为)?(?=\d+(?:\.\d+)?元)"
)


def normalize_literal_order_syntax(text: str) -> str:
    """Only 公司(的)订单(为)<amount>; never drop subjects, modals or qualifiers."""
    return _LITERAL_ORDER.sub(r"\1订单", text)


def strip_reported_speech_prefixes(text: str) -> str:
    """Alternate source view for explicit attributions, never extra numeric evidence.

    Keep denials, plans, hearsay and ordinary entity headings attached. Replacing
    rather than appending clauses preserves the one-to-one numeric match rule.
    """
    def strip(match):
        head = match[2].strip()
        if re.search(r'不|未|无|否认|澄清|辟谣|传闻|谣言|虚假|不实|驳斥|计划|拟|可能|预计|预测|假设|示例|预期', head):
            return match[0]
        if re.fullmatch(r'(?:(?:中国)?证监会|央行|中国人民银行|财政部|商务部|国家统计局)[\u4e00-\u9fff]{0,6}|.{1,24}(?:表示|指出|介绍|称|透露|宣布)', head):
            return match[1]
        return match[0]
    return re.sub(r'(^|[。；;\n])([^：:\n。；;]{1,30})[：:]', strip, text)


_DENIAL_MARKERS = frozenset({"尚未", "未曾", "没有", "否认", "并未", "不会", "未"})
_PLAN_MARKERS = frozenset({"拟", "计划", "将"})
_FORECAST_MARKERS = frozenset({"预计", "有望", "可能", "或将"})


def _event_polarity(markers: list[str]) -> tuple[str, ...]:
    """Keep every recognized modal layer; a forecast cannot erase a denial/plan."""
    polarity = []
    for marker in markers:
        if marker in _DENIAL_MARKERS:
            polarity.append("denied")
        elif marker in _PLAN_MARKERS:
            polarity.append("planned")
        elif marker in _FORECAST_MARKERS:
            polarity.append("forecast")
        else:
            polarity.append("asserted")
    return tuple(polarity) or ("asserted",)


# A punctuation mark always ends a clause. 、 and 并 end one only when a fresh,
# explicit company subject and event verb follow, so ordinary object lists stay
# whole. Protect the listed merger words lexically, even after an earlier event:
# cutting 合并/兼并/吞并/归并/并购 invents an event owner or loses an acquisition.
# A real connector may also start a new sentence ("。并乙公司获得订单").
_CLAUSE_BOUNDARY = re.compile(
    r"[,，。；;！!？?\n]"
    r"|(?P<boundary>、|(?<![合兼吞归])并(?!购)(?:且|同时)?)(?=[\u4e00-\u9fffA-Za-z0-9]{1,40}公司(?:的)?"
    r"(?:已|未|拟|将|计划|预计|可能|不会)?(?:" + _EVENT.pattern + r"))"
)


def _split_clauses(text: str) -> list[str]:
    """Protect merger words and consume complete 并/并且/并同时 connectors."""
    clauses: list[str] = []
    start = 0
    for match in _CLAUSE_BOUNDARY.finditer(text):
        clauses.append(text[start : match.start()])
        start = match.end()
    clauses.append(text[start:])
    return clauses


# Overt connectors that may join a second predicate to the same subject. A trailing
# run of these plus modal markers belongs to the next verb, not to this object.
_PREDICATE_TAIL_TOKENS = (
    "并且", "并同时", "以及", "同时", "然后", "随后", "并", "且",
    "和", "与", "及", "、", "但", "却", "而",
)


def _split_predicate_tail(text: str) -> tuple[str, list[str]]:
    """Separate one verb's object from a connector and the next verb's modals.

    A modal travels with the next verb only when a connector sits between the
    object and that modal ("建设工厂并计划获得订单"). "并购计划" is a noun: 计划
    stays in the earlier object unless a connector introduces it as a modifier.
    """
    adjacent: list[str] = []
    while text:
        state = _STATE.search(text)
        if not state:
            break
        adjacent.insert(0, state[0])
        text = text[: state.start()]
    connector = False
    changed = True
    while changed and text:
        changed = False
        for token in _PREDICATE_TAIL_TOKENS:
            if text.endswith(token):
                text = text[: -len(token)]
                connector = True
                changed = True
                break
    if connector:
        return text, adjacent
    # No connector: 计划/拟/将 in front of a denial are the noun 并购计划,
    # not a plan mood for the following verb.
    denial = [marker for marker in adjacent if marker in _DENIAL_MARKERS]
    noun = [marker for marker in adjacent if marker not in _DENIAL_MARKERS]
    return text + "".join(noun), denial


_DENIED_RUMOR_TAIL = re.compile(r"的(?:传闻|消息)(?:已)?被否认$")


def _non_completed_event(clause: str, matches: list[re.Match], index: int) -> bool:
    """Do not turn a plan, approval, or qualification into an executed event.

    The patterns are limited to acquisition plans and adjacent permission or
    qualification phrases, not a general Chinese event parser. A conjunction
    such as "获批并收购" is an independent predicate; "获批收购" and
    "获得收购...的批准" state permission, not execution. The later 获批 in
    "并购未获批" is still extracted and checked independently.
    """
    match = matches[index]
    verb = _EVENT_VERB_CANONICAL.get(match[0], match[0])
    if verb not in {"收购", "签订", "中标"}:
        return False
    next_start = matches[index + 1].start() if index + 1 < len(matches) else len(clause)
    event_tail = clause[match.end() : next_start]
    if verb == "收购" and (event_tail.startswith("计划") or event_tail.endswith("的计划")):
        return True
    if index == 0:
        return False
    previous = matches[index - 1]
    previous_verb = _EVENT_VERB_CANONICAL.get(previous[0], previous[0])
    between = clause[previous.end() : match.start()]
    if between or previous_verb not in {"获得", "获批"}:
        return False
    if previous_verb == "获批":
        return verb in {"收购", "签订"}
    if verb == "中标":
        return event_tail.endswith("资格")
    return bool(re.search(r"的(?:批准|批复|授权)(?:$|并|且|，|、)", event_tail))


def _event_claims(text: str, *, allow_subjectless: bool = False):
    """Narrow literal event statements, not a general semantic entailment model.

    Every event verb in a clause is extracted, not only the first one, so a
    conjoined second predicate is visible in both directions: a reason may repeat
    a verb the source really states, and it may not add one the source never does.
    """
    text = strip_reported_speech_prefixes(unicodedata.normalize('NFKC', text))
    for clause in _split_clauses(text):
        clause = re.sub(r'\s+', '', clause).replace(':', '')
        forecast = _FORECAST_PREFIX.match(clause)
        if forecast:
            clause = clause[forecast.end() :]
        matches = list(_EVENT.finditer(clause))
        if not matches:
            continue
        subject = clause[: matches[0].start()]
        markers = []
        state = _STATE.search(subject)
        while state:
            markers.insert(0, state[0])
            subject = subject[: state.start()]
            state = _STATE.search(subject)
        if forecast:
            markers.insert(0, forecast[0])
        if (not subject and not allow_subjectless) or _INFERENCE.search(subject):
            continue
        carried: list[str] = []
        for index, match in enumerate(matches):
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(clause)
            )
            raw_tail = clause[match.end() : end]
            obj, next_markers = _split_predicate_tail(raw_tail)
            # An interpretation AFTER a fact must not erase its asserted polarity.
            obj = _INFERENCE.split(obj, maxsplit=1)[0]
            verb = _EVENT_VERB_CANONICAL.get(match[0], match[0])
            if _non_completed_event(clause, matches, index):
                carried = [marker for marker in next_markers if marker in _DENIAL_MARKERS]
                continue
            # _split_predicate_tail treats a trailing 否认 as a modal for a
            # *following* verb. In "获得订单的传闻已被否认" it instead denies this
            # very event, so inspect the unstripped tail.
            rumor = _DENIED_RUMOR_TAIL.search(raw_tail)
            if rumor:
                obj = raw_tail[: rumor.start()]
                # A rumor's denial is not evidence that the rumored event took
                # place, even if the denial follows the event words.
                polarity = _event_polarity(markers + carried + ["否认"])
            else:
                polarity = _event_polarity(markers + carried)
            # Only modals that _split_predicate_tail kept as direct modifiers of
            # the next verb are carried. Noun 计划 stays on the earlier object.
            yield subject, verb, obj, polarity
            carried = list(next_markers)


def _event_object_comparison_text(text: str) -> str:
    """Normalize only literal possessive/prepositional object grammar."""
    text = _event_money_comparison_text(text)
    named = re.fullmatch(
        r"(?P<with>与)?(?P<owner>[\u4e00-\u9fffA-Za-z0-9]{1,40}?(?:公司|集团))"
        r"的?(?P<object>[^，,；;。\n]*(?:订单|合同|补助|批复))",
        text,
    )
    if named and (not named["with"] or named["object"].endswith("合同")):
        # Keep the entire named counterparty and every object qualifier. 与 is
        # only the contract preposition, never a reason to erase another owner.
        text = named["owner"] + named["object"]
    return re.sub(r"^该(?=项目订单$|采购合同$|订单$|合同$)", "", text)


def _event_objects_related(source_obj: str, logic_obj: str) -> bool:
    """A source may support a less specific object, but not invent its own.

    An empty source object can result from an embedded approval phrase and
    cannot ground a reason that adds a concrete order, company, or contract.
    """
    if not source_obj:
        return not logic_obj
    if not logic_obj:
        return True
    source_obj = _event_object_comparison_text(source_obj)
    logic_obj = _event_object_comparison_text(logic_obj)
    # Keep the established qualitative order paraphrase, but never rewrite
    # names or infer magnitude from an amount. A shared noun is not evidence
    # that two named counterparties or their contract terms are interchangeable.
    source_obj = re.sub(r"^重大(?=订单$|合同$)", "大额", source_obj)
    logic_obj = re.sub(r"^重大(?=订单$|合同$)", "大额", logic_obj)
    if source_obj == logic_obj:
        return True
    # Only a bare category may omit source detail. Arbitrary substring matching
    # would turn 非独家订单 into 独家订单, or 子公司 into its parent company.
    return logic_obj in {"订单", "合同", "补助", "批复"} and source_obj.endswith(logic_obj)


def explicit_event_claims_are_grounded(logic: str, source: str) -> bool:
    """Reject literal entity/negation/modality flips where source statements overlap.

    获得/签订/中标/获批/收购 in the reason are also ungrounded when the source
    text never states that verb. Other recognized verbs stay on the overlap rule,
    because words such as 投资 also appear in non-event commentary. Paraphrases,
    elided second verbs, and unrecognized predicates remain outside this bounded
    check; it must not be described as a complete fact verifier.
    """
    sources = list(_event_claims(source, allow_subjectless=True))
    for subject, verb, obj, state in _event_claims(logic, allow_subjectless=True):
        related = [
            s for s in sources
            if s[1] == verb and _event_objects_related(s[2], obj)
        ]
        if not related:
            if verb in _SOURCE_REQUIRED_EVENTS:
                return False
            continue
        # A subject-less reason is still an event claim. It cannot borrow an
        # unrelated object or an ambiguous owner.
        if not subject:
            matching = [
                item for item in related if _polarity_compatible(item[3], state)
            ]
            owners = {item[0] for item in matching if item[0]}
            if not matching or len(owners) > 1:
                return False
            continue
        # A subject-less source denial still contradicts an affirmative reason.
        if any(not s[0] and not _polarity_compatible(s[3], state) for s in related):
            return False
        named = [s for s in related if s[0]]
        if not named:
            continue
        owners = {s[0] for s in named}
        if subject in _GENERIC_SUBJECTS and len(owners) == 1:
            subject = next(iter(owners))
        matching = [s for s in named if s[0] == subject]
        if not matching or any(not _polarity_compatible(s[3], state) for s in matching):
            return False
    return True


_REUSE_EVENT_VERBS = _SOURCE_REQUIRED_EVENTS | {"增持", "减持"}


def _polarity_compatible(left, right) -> bool:
    return frozenset(left) == frozenset(right)


_GROWTH_MARKERS = ("增长", "上升", "提高", "增加", "上涨", "同比增")
_DECLINE_MARKERS = ("下降", "下跌", "下滑", "减少", "降低", "同比降")


def _growth_or_decline(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    up = any(marker in normalized for marker in _GROWTH_MARKERS)
    down = any(marker in normalized for marker in _DECLINE_MARKERS)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return ""


_REUSE_FINANCIAL_METRIC = re.compile(
    r"归母净利润|扣非净利润|营业总收入|营业收入|营收|净利润|毛利润|利润"
)
_REUSE_FINANCIAL_TREND = re.compile(r"增长|增加|上升|提高|下降|减少|下滑|降低|增|降")
_REUSE_FINANCIAL_STATE = re.compile(_STATE.pattern.removesuffix("$"))
_REUSE_FINANCIAL_CLAIM = re.compile(
    r"(?P<metric>" + _REUSE_FINANCIAL_METRIC.pattern + r")"
    r"(?P<bridge>(?:[:：]|同比|环比|" + _REUSE_FINANCIAL_STATE.pattern + r"){0,6})"
    r"(?P<trend>" + _REUSE_FINANCIAL_TREND.pattern + r")"
    r"(?P<qualifier>不超过|超过|至少|至多|约|近|逾|超)?"
    r"(?P<amount>[+-]?\d+(?:\.\d+)?(?:个百分点|人民币|美元|港元|欧元|日元|英镑|%|元|倍)?)"
)
_REUSE_PERIOD = re.compile(
    r"(?:\d{4}年)?(?:前[一二三123]季度|第?[一二三四1234]季度|[上下]半年|全年|年度)"
    r"|\d{4}年"
)
_REUSE_REPORTING_TAIL = re.compile(r"(?:公告|披露|发布|实现|录得|显示|的)+$")
_REUSE_ISSUER_PREDICATE = re.compile(
    r"发布|披露|公告|净利润|营业收入|营收|董事会|控股股东|" + _EVENT.pattern
)
_REUSE_SOURCE_PREFIX = re.compile(
    r"^(?:【(?:财联社|格隆汇|金十数据|华尔街见闻|新华社|证券时报)】"
    r"|(?:财联社|格隆汇|金十数据|华尔街见闻|新华社|证券时报)[:,])"
)


def _strip_reuse_source_prefix(text: str) -> str:
    """Only named publisher labels; never remove dates or arbitrary brackets."""
    return _REUSE_SOURCE_PREFIX.sub("", unicodedata.normalize("NFKC", text).strip())


def _reuse_financial_context(prefix: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Read only literal headline prefixes; never guess company-name aliases."""
    periods = tuple(_REUSE_PERIOD.findall(prefix))
    prefix = _REUSE_PERIOD.sub("", prefix).strip(":")
    prefix = _REUSE_REPORTING_TAIL.sub("", prefix).strip(":")
    states = []
    state = _STATE.search(prefix)
    while state:
        states.insert(0, state[0])
        prefix = prefix[:state.start()].rstrip(":")
        state = _STATE.search(prefix)
    return prefix, periods, _event_polarity(states)


def _reuse_financial_claims(text: str) -> Counter:
    """Bind a bounded financial metric to its owner, period and numeric claim.

    Clause order and listed metric/trend synonyms may change. The first named
    owner/period can govern later subjectless clauses, but a new explicit owner
    replaces it. Unrecognized prose remains under the existing event guard.
    """
    from core.scorer import _numeric_facts

    claims = Counter()
    owner, period = "", ()
    for clause in _split_clauses(text):
        previous_end = 0
        for match in _REUSE_FINANCIAL_CLAIM.finditer(clause):
            prefix = re.sub(
                r"^(?:、|以及|同时|并且|并|且)+", "", clause[previous_end:match.start()]
            )
            named, stated_period, state = _reuse_financial_context(prefix)
            if named:
                owner = named
            if stated_period:
                period = stated_period
            bridge_states = _REUSE_FINANCIAL_STATE.findall(match["bridge"])
            if bridge_states:
                state = (() if state == ("asserted",) else state) + _event_polarity(bridge_states)
            metric = "营业收入" if match["metric"] == "营收" else match["metric"]
            trend = "up" if match["trend"] in (*_GROWTH_MARKERS, "增") else "down"
            facts = tuple(sorted(_numeric_facts(match["amount"])))
            claims[(
                owner, period, metric, tuple(re.findall(r"同比|环比", match["bridge"])), trend,
                match["qualifier"] or "", facts, state,
            )] += 1
            previous_end = match.end()
    return claims


def _unbound_mixed_financial_claims(text: str) -> bool:
    """An unparsed mixed-metric statement is not evidence of equivalence."""
    metrics = {match.start() for match in _REUSE_FINANCIAL_METRIC.finditer(text)}
    if len(metrics) < 2:
        return False
    directions = {
        "up" if match[0] in (*_GROWTH_MARKERS, "增") else "down"
        for match in _REUSE_FINANCIAL_TREND.finditer(text)
    }
    bound = {match.start() for match in _REUSE_FINANCIAL_CLAIM.finditer(text)}
    return len(directions) > 1 and bool(metrics - bound)


def _reuse_explicit_issuer(text: str) -> str:
    """Compare an explicit issuer even for non-event headlines like reports."""
    first_clause = _split_clauses(text)[0]
    predicate = _REUSE_ISSUER_PREDICATE.search(first_clause)
    if predicate is None:
        return ""
    subject, _periods, _state = _reuse_financial_context(first_clause[:predicate.start()])
    return "" if subject in _GENERIC_SUBJECTS else subject


def event_reuse_conflicts(new_text: str, old_text: str) -> bool:
    """Similarity may find a candidate; reuse still needs the same event facts.

    A title that flips negation, plan, growth/decline, subject, or numbers
    must be scored again. Missing an old amount is a conflict, not a match.
    """
    new_evidence, old_evidence = (
        re.sub(r"\s+", "", _event_money_comparison_text(
            _strip_reuse_source_prefix(str(text or ""))
        ))
        for text in (new_text, old_text)
    )
    new_issuer, old_issuer = (
        _reuse_explicit_issuer(new_evidence), _reuse_explicit_issuer(old_evidence)
    )
    if new_issuer and old_issuer and new_issuer != old_issuer:
        return True
    if new_evidence != old_evidence and (
        _unbound_mixed_financial_claims(new_evidence)
        or _unbound_mixed_financial_claims(old_evidence)
    ):
        return True
    if _reuse_financial_claims(new_evidence) != _reuse_financial_claims(old_evidence):
        return True
    new_claims = list(_event_claims(new_evidence, allow_subjectless=True))
    old_claims = list(_event_claims(old_evidence, allow_subjectless=True))

    def unmatched(source_claims, target_claims) -> bool:
        for subject, verb, obj, state in source_claims:
            if verb not in _REUSE_EVENT_VERBS:
                continue
            same = [
                item for item in target_claims
                if item[1] == verb
                and _polarity_compatible(item[3], state)
                and _event_objects_related(item[2], obj)
                and (not subject or not item[0] or item[0] == subject)
            ]
            if not same:
                return True
        return False

    if unmatched(new_claims, old_claims) or unmatched(old_claims, new_claims):
        return True
    new_dir, old_dir = _growth_or_decline(new_text), _growth_or_decline(old_text)
    if new_dir and old_dir and new_dir != old_dir:
        return True
    try:
        from core.scorer import _numeric_facts
    except ImportError:
        return False
    new_nums, old_nums = _numeric_facts(new_evidence), _numeric_facts(old_evidence)
    return bool(new_nums - old_nums or old_nums - new_nums)


def prepare_reuse_identity_index(rows):
    """Index literal master identities for a scoring veto, never asset mapping.

    Only master-confirmed names/aliases and explicitly labelled A-share codes
    are eligible. Unknown/ambiguous input means another score, not a guessed
    company. This pure decision code is part of the scoring fingerprint.
    """
    names, codes, canonical = {}, {}, {}
    for security_id, name, ticker, exchange, aliases in rows or ():
        if not security_id or not name or not re.fullmatch(r"[0-9]{6}", str(ticker)):
            continue
        if exchange not in {"SSE", "SZSE", "BSE"}:
            continue
        canonical[security_id] = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name))
        try:
            aliases = json.loads(aliases) if isinstance(aliases, str) else aliases
        except (TypeError, ValueError):
            return None
        if not isinstance(aliases, list):
            return None
        for text in [name, *aliases]:
            if not isinstance(text, str):
                continue
            text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
            if len(text) >= 2 and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]+", text):
                names.setdefault(text, set()).add(security_id)
        codes.setdefault((exchange, str(ticker)), set()).add(security_id)
    if not names:
        return None
    pattern = re.compile("|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)))
    return names, codes, canonical, pattern


_REUSE_MARKETS = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
_REUSE_QUALIFIED_CODE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<market>SH|SZ|BJ)[.:]?(?P<prefix>[0-9]{6})"
    r"|(?P<suffix>[0-9]{6})[.](?P<market2>SH|SZ|BJ)"
    r"|(?:证券|股票|A股)代码[:=]?(?P<label>[0-9]{6}))(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_REUSE_ENTITY_TAIL = re.compile(
    r"^(?:$|[:：,，。;；!！?？()（）]|董事长|董事会|总经理|股东|控股股东|"
    r"净利润|营业收入|营收|拟|将|已|预计|发布|披露|公告|"
    r"(?:的)?(?:订单|(?:年度)?(?:设备)?采购合同)|" + _EVENT.pattern + r")"
)


def _reuse_identity_view(text, index):
    names, codes, canonical, pattern = index
    identities, first_prefix, invalid = [], None, False

    def replace_name(match):
        nonlocal first_prefix, invalid
        owners = names[match[0]]
        if len(owners) != 1 or not _REUSE_ENTITY_TAIL.match(text[match.end():]):
            invalid = True
            return match[0]
        owner = next(iter(owners))
        identities.append(owner)
        if first_prefix is None:
            first_prefix = text[:match.start()]
        return canonical[owner]

    view = pattern.sub(replace_name, text)

    def replace_code(match):
        nonlocal invalid
        ticker = match["prefix"] or match["suffix"] or match["label"]
        market = match["market"] or match["market2"]
        owners = codes.get((_REUSE_MARKETS[market.upper()], ticker), set()) if market else set().union(
            *(ids for (_exchange, code), ids in codes.items() if code == ticker)
        )
        if len(owners) != 1:
            invalid = True
            return match[0]
        owner = next(iter(owners))
        identities.append(owner)
        return canonical[owner]

    view = _REUSE_QUALIFIED_CODE.sub(replace_code, view)
    return view, tuple(identities), first_prefix, invalid


def reuse_identity_comparison(new_title: str, old_title: str, index):
    """Return comparable titles only when issuer identity is sufficiently known.

    Exact publisher-title repeats (including non-company macro news) require no
    master lookup. Other unknown rewrites fail closed. Explicit same literal
    subjects already recognized by the event parser remain eligible, but the
    original event/numeric/reason checks must still run on original titles
    afterward. Canonicalized aliases must never widen their accepted cases.
    """
    new, old = (
        re.sub(r"\s+", "", _strip_reuse_source_prefix(str(title or "")))
        for title in (new_title, old_title)
    )
    if new and new.removesuffix("。") == old.removesuffix("。"):
        return new, old
    if index is None:
        return None
    new_view, new_ids, new_prefix, new_invalid = _reuse_identity_view(new, index)
    old_view, old_ids, old_prefix, old_invalid = _reuse_identity_view(old, index)
    if new_invalid or old_invalid or new_ids != old_ids:
        return None
    if not new_ids:
        # A common media/reporting prefix is not proof that the actual company
        # is unchanged. Only the already guarded, explicit event declarations
        # remain candidates; original bidirectional object checks run next.
        claims = [list(_event_claims(text, allow_subjectless=True)) for text in (new, old)]
        if any(not group or any(not subject or verb not in _REUSE_EVENT_VERBS
                               for subject, verb, _obj, _state in group) for group in claims):
            return None
        return (new, old) if {item[0] for item in claims[0]} == {item[0] for item in claims[1]} else None
    if len(set(new_ids)) > 1:
        claims = [list(_event_claims(text, allow_subjectless=True)) for text in (new_view, old_view)]
        if any(not group or any(not item[0] for item in group) for group in claims):
            return None
        if {item[0] for item in claims[0]} != {item[0] for item in claims[1]}:
            return None
    elif new_prefix != old_prefix:
        return None
    return new_view, old_view


def similar_logic_ungrounded(title: str, source_summary: str, similar_info: dict) -> bool:
    """Old similar reasons must still ground in the new publisher source."""
    from core.scorer import _score_grounding_failure

    parsed = (
        similar_info.get("score") or 0,
        str(similar_info.get("tag") or ""),
        str(similar_info.get("logic") or ""),
        str(similar_info.get("direction") or "中性"),
    )
    return bool(_score_grounding_failure(
        parsed, _strip_reuse_source_prefix(title), source_summary
    ))


_LOW_SCORE_DENIAL_BOUNDARY = re.compile(r'([，,。；;！!？?\r\n])')
# These two observed noun-only denials have no existing category suffix.  Keep
# the finite forms separate: extending the generic scope/category grammar could
# hide an event, number, another theme, or text following the denial.
_LOW_SCORE_INDEPENDENT_DENIAL_TAILS = frozenset({
    '非宏观流动性焦点',
    '无宏观政策增量',
})
# Connectors that open a new assertion.  A denial scope never crosses one; the
# text from the connector on is kept for theme checking.  并购/并网/偿还… are
# nouns, not connectors.  以及/及/和/与/、/或 only coordinate denied objects
# (a predicate after them is caught by the open-form scope check).
_ASSERTION_CONNECTORS = (
    r'并且|而且|同时|另外|此外|随后|此后|其后|但是|然而|不过|而是|只是|'
    r'并(?!购|网|表|列|发)|且|而|但|却|又|还(?!款|原|债|贷)'
)
_ASSERTION_CONNECTOR_RE = re.compile(_ASSERTION_CONNECTORS)
# "并非与/不是与 X 无关" is a double negation (an affirmative link): keep it.
# 并非/不是 before a relation or a negation is a double negation (affirmative):
# "并非与X无关", "并非没有X信息", "不是无X影响".  Such clauses are never stripped.
# Any whitespace (ASCII, tab, full-width U+3000) may separate the two negators.
_DOUBLE_NEGATION_PREFIX = r'(?:非|不是)'
_NEGATOR_GAP = r'[\s\u3000]*'
_DOUBLE_NEGATION_RE = re.compile(
    rf'{_DOUBLE_NEGATION_PREFIX}{_NEGATOR_GAP}'
    r'(?:与|和|同|没有|没|无|未见|未提供|未涉及|未涉|缺乏|不涉及|不属于|不包含)'
)
# Text ending in 非/不是 (+ whitespace): a following negation is double-negated.
_DOUBLE_NEGATOR_TAIL_RE = re.compile(rf'{_DOUBLE_NEGATION_PREFIX}{_NEGATOR_GAP}$')
# Announcement/action verbs mean the "scope" carries a claim: fail closed.
_ASSERTION_VERBS = (
    r'宣布|宣称|表示|声称|称|发布|推出|出台|决定|批准|实施|启动|'
    r'下调|上调|调降|调升|将|拟|已|正在|计划'
)
# No "有关/相关/无关" (double negation such as 并非与X无关的事件) and no
# positive-change predicate either.
_DENIAL_SCOPE_BLOCKERS = (
    r'仍|有关|相关|无关|'
    r'改善|宽松|释放|利好|提振|提升|增加|上升|增长|回暖|充裕|放水|扩张|加码|突破|大增'
)
# A denial scope is a bounded noun list: no clause punctuation, connector,
# assertion verb or blocker.
_DENIAL_SCOPE = (
    rf'(?:(?!{_ASSERTION_CONNECTORS}|{_ASSERTION_VERBS}|{_DENIAL_SCOPE_BLOCKERS})'
    r'[\u4e00-\u9fffa-z0-9、/&·\- \t]){1,60}'
)
_SCOPED = rf'(?P<scope>{_DENIAL_SCOPE})'
_DENIAL_CATEGORY = r'(?:事件|消息|新闻|政策|范畴|范围|主线|题材|催化|事项|制度|异动|影响|映射|关联|落地|信号)'
_CLOSED_DENIAL_END = r'(?:无关|无直接关联|无直接关系|无关联|没有关系|没有关联|无关系)'
# (rule, closed).  Closed forms carry an explicit relation end marker, so the
# denied object list is bounded by the grammar itself.  Open forms (不涉及 X,
# 非/无 X <generic category>) need the strict item check below.
_LOW_SCORE_DENIAL_CLAUSES = (
    # 与/和 [A股][宏观] X 及 Y 等 Z 无关 / 无直接关联 / 没有关系
    (
        re.compile(
            rf'(?:且|并|也|均)?(?:与|和|同){_SCOPED}{_CLOSED_DENIAL_END}',
            re.IGNORECASE,
        ),
        True,
    ),
    # 对 [A股] X 无(直接)影响
    (
        re.compile(
            rf'(?:且|并|也|均)?对{_SCOPED}无(?:直接|明显|实质)?(?:影响|关联)',
            re.IGNORECASE,
        ),
        True,
    ),
    # 非 / 而非 / 并非 … 事件|政策…  (非农/非银/非常… are not negations)
    (
        re.compile(
            rf'(?:而|并)?非(?!农|银|常|标|洲|美|公|法|凡){_SCOPED}{_DENIAL_CATEGORY}',
            re.IGNORECASE,
        ),
        False,
    ),
    # 不涉及 / 未涉及 / 不属于 / 不包含 … 列表
    (
        re.compile(
            rf'(?:且|并|也|均)?(?:不涉及|未涉及|未涉|不属于|不包含){_SCOPED}',
            re.IGNORECASE,
        ),
        False,
    ),
    # 无 / 并无 / 没有 … 列表 … 政策|异动|影响  (无人机/无线/无疑… are not negations)
    (
        re.compile(
            rf'(?:且|并|也|均)?(?:并无|没有|无(?!论|疑|需|须|限|风险|人|线|锡|机|源|形|缝|损))'
            rf'{_SCOPED}{_DENIAL_CATEGORY}',
            re.IGNORECASE,
        ),
        False,
    ),
    # Explicit negative information: 无/没有/未见/未提供/缺乏 [新增] X [增量|新增|相关] 信息.
    # Open form: the scope gets the strict item check; 信息 alone never exempts.
    (
        re.compile(
            rf'(?:且|并|也|均)?(?:并无|没有|未见|未提供|缺乏|'
            rf'无(?!论|疑|需|须|限|风险|人|线|锡|机|源|形|缝|损))'
            rf'(?:新增)?{_SCOPED}(?:增量|新增|相关)?信息',
            re.IGNORECASE,
        ),
        False,
    ),
)
# "<subject>与 [A股] X 无关" at the end of a clause (closed); the subject is kept.
_SUBJECT_DENIAL_TAIL = re.compile(
    rf'(?<=[\u4e00-\u9fffa-z0-9])(?:与|和|同)(?!.*(?:与|和|同)){_SCOPED}'
    rf'{_CLOSED_DENIAL_END}$',
    re.IGNORECASE,
)


# An open-form scope is a list of objects joined by explicit list separators.
# Each item must be short and name at most one theme group or event term, and
# an item after a separator must not be a predicate (央行降准); otherwise
# ("不涉及AI央行降准", "不涉及AI以及央行降准") the scope is ambiguous and the
# clause is kept whole for theme checking.
_DENIAL_SCOPE_ITEM_SEPARATOR = re.compile(r'[、/&·\- \t]|以及|及|和|或|与|等')
# Actor followed by an action: a new statement, not a coordinated object.
_DENIAL_SCOPE_PREDICATE = re.compile(
    r'(?:央行|人民银行|美联储|欧央行|政府|国务院|证监会|财政部|发改委|工信部|商务部|'
    r'监管|部委|公司|企业|厂商|银行|交易所|北向|外资)'
    r'.{0,4}?'
    r'(?:降准|降息|加息|缩表|扩表|注资|干预|买入|卖出|回购|增持|减持|涨价|提价|降价|'
    r'减产|增产|停产|复产|扩产|投产|签约|裁员|中标|获批|上市|收紧|下调|上调)'
)
_DENIAL_SCOPE_ITEM_MAX_CHARS = 12
_DENIAL_SCOPE_EVENT_TERMS = (
    '出口', '进口', '回暖', '订单', '中标', '涨价', '提价', '降价', '减产', '增产',
    '停产', '复产', '扩产', '投产', '获批', '上市', '并购', '回购', '增持', '减持',
    '签约', '裁员', '交付', '涨停', '跌停', '大涨', '大跌', '反弹', '业绩',
)
ThemeGroups = tuple[tuple[str, ...], ...]


def _term_pattern(term: str) -> str:
    escaped = re.escape(term.casefold())
    if term.isascii() and term.isalnum():
        return rf'(?<![a-z0-9]){escaped}(?![a-z0-9])'
    return escaped


def _scope_item_term_ids(item: str, theme_groups: ThemeGroups) -> set:
    """Theme groups and distinct event terms named by one scope item."""
    ids = set()
    for index, group in enumerate(theme_groups):
        terms = sorted(group, key=len, reverse=True)
        pattern = '|'.join(_term_pattern(term) for term in terms)
        if pattern and re.search(pattern, item, re.IGNORECASE):
            ids.add(('theme', index))
            # A theme term such as 新药上市 must not also count as event 上市.
            item = re.sub(pattern, '\x00', item, flags=re.IGNORECASE)
    ids.update(('event', term) for term in _DENIAL_SCOPE_EVENT_TERMS if term in item)
    return ids


def _open_denial_scope_is_bounded(scope: str, theme_groups: ThemeGroups) -> bool:
    for index, item in enumerate(_DENIAL_SCOPE_ITEM_SEPARATOR.split(scope)):
        if len(item) > _DENIAL_SCOPE_ITEM_MAX_CHARS:
            return False
        if len(_scope_item_term_ids(item, theme_groups)) > 1:
            return False
        if index and _DENIAL_SCOPE_PREDICATE.search(item):
            return False
    return True


def _is_denial_clause(text: str, theme_groups: ThemeGroups) -> bool:
    for rule, closed in _LOW_SCORE_DENIAL_CLAUSES:
        match = rule.fullmatch(text)
        if match and (
            closed or _open_denial_scope_is_bounded(match.group('scope'), theme_groups)
        ):
            return True
    return False


def _strip_denial_clause(clause: str, theme_groups: ThemeGroups) -> str:
    """Return the clause minus its leading denial; unchanged when ambiguous."""
    if _DOUBLE_NEGATION_RE.search(clause):
        return clause
    if _is_denial_clause(clause, theme_groups):
        return ''
    for connector in _ASSERTION_CONNECTOR_RE.finditer(clause):
        head = clause[: connector.start()]
        if head and _is_denial_clause(head, theme_groups):
            # Strip only the negated scope; the new assertion is kept unless it
            # is itself a complete denial ("不涉及X且与Y无关").
            rest = _strip_denial_clause(clause[connector.end():], theme_groups)
            return connector.group() + rest if rest.strip() else ''
    tail = _SUBJECT_DENIAL_TAIL.search(clause)
    if tail:
        # Keep the subject: any theme marker in it is still checked.
        return clause[: tail.start()]
    return clause


def strip_low_score_denial_clauses(text: str, theme_groups: ThemeGroups) -> str:
    """Drop only explicit, clearly bounded negated scopes, for low neutral logic.

    Within each clause (between punctuation boundaries) a bounded denial form
    is removed only up to a connector that opens a new assertion (并且/同时/
    但/且…); that assertion is kept verbatim so theme markers in it are still
    checked.  Closed forms (与X无关/对X无影响) strip X as written; open forms
    (不涉及X, 非/无X事件) need X to be a list of short items, each naming at most
    one of ``theme_groups`` or one event term, with no predicate after a list
    separator.  A scope that fails, or that holds a connector, announcement
    verb, contrast or positive-change predicate, is ambiguous and the clause
    is kept whole.  Callers must not apply this to tags,
    sources or scores above 4.  Two finite independent tail forms are additionally
    allowed only as the final non-punctuation clause; no other scope is inferred.
    """
    parts = _LOW_SCORE_DENIAL_BOUNDARY.split(text)
    tail_index = next(
        (index for index in range(len(parts) - 1, -1, -1)
         if parts[index].strip() and not _LOW_SCORE_DENIAL_BOUNDARY.fullmatch(parts[index])),
        -1,
    )
    kept = []
    for index, part in enumerate(parts):
        clause = part.strip(' \t')
        if clause and not _LOW_SCORE_DENIAL_BOUNDARY.fullmatch(part):
            stripped = (
                '' if index == tail_index and clause in _LOW_SCORE_INDEPENDENT_DENIAL_TAILS
                else _strip_denial_clause(clause, theme_groups)
            )
            if stripped != clause:
                part = stripped
        kept.append(part)
    return ''.join(kept)


def _drop_unless_double_negated(match: re.Match) -> str:
    """Keep a negation preceded by 并非/不是 (any whitespace between): it affirms X."""
    if _DOUBLE_NEGATOR_TAIL_RE.search(match.string, 0, match.start()):
        return match.group()
    return ''


def strip_explicit_theme_denials(text: str, markers: tuple[str, ...]) -> str:
    """Remove only a narrow negated list, never a following contrast/positive claim.

    Callers may exempt low-score neutral *logic*, never an affirmative tag.
    Source-side denials cannot ground an affirmative theme assertion either.
    This is not general-purpose negation/entailment inference.
    """
    atoms = []
    for marker in sorted(markers, key=len, reverse=True):
        escaped = re.escape(marker.casefold())
        if marker.isascii() and marker.isalnum():
            escaped += r'(?![a-z0-9])'
        atoms.append(escaped)
    if not atoms:
        return text
    atom = '(?:' + '|'.join(atoms) + ')'
    pattern = rf'(?:不涉及|未涉及|不属于|不包含|没有)(?:任何)?\s*{atom}(?:\s*(?:或|及|和|与|、|/)\s*{atom})*'
    text = re.sub(pattern, _drop_unless_double_negated, text, flags=re.IGNORECASE)
    # Only a complete standalone "与 [A股][宏观] <known themes> 无关" clause.
    # No wildcards/newline whitespace: keep surrounding and contrast claims.
    # A clause boundary also excludes double negations such as "并非与...无关".
    boundary = r'[，,。；;！!？?\r\n]'
    postfix = (
        rf'(^|{boundary})([ \t]*)与[ \t]*(?:A股[ \t]*)?(?:宏观[ \t]*)?{atom}'
        rf'(?:[ \t]*(?:或|及|和|与|、|/)[ \t]*{atom})*[ \t]*无关'
        rf'(?=$|{boundary})'
    )
    return re.sub(postfix, r'\1\2', text, flags=re.IGNORECASE)
