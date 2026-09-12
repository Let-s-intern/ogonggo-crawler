"""뽑는 칸을 원문 그대로 옮긴다.

모델에게 문장을 쓰게 하면 "그대로 옮겨라" 가 부탁에 그친다. 단어 하나를 바꾸거나 두 줄을
합쳐도 막을 길이 없다. 그래서 모델은 **몇 번 줄의 어느 부분인지** 만 답하고, 저장하는 글자는
이 파일이 원문에서 잘라 온다.

## 줄 번호

`[0]` 이 제목이고 `[1]` 부터가 본문이다. 빈 줄은 번호를 받지 않는다 — 원문을 편 글에는
빈 줄이 많고, 번호만 붙은 빈 줄은 토큰만 쓴다.

## 조각 하나를 옮기는 순서

| 순서 | 무엇을 보나 | 저장하는 것 |
|---|---|---|
| 1 | 모델이 짚은 줄에 그 부분이 있다 | 원문에서 그 부분 |
| 2 | 짚은 줄엔 없지만 다른 줄에 있다 | 원문에서 그 부분. 번호만 틀린 경우다 |
| 3 | 원문 어디에도 없고 짚은 줄은 있다 | **짚은 줄 전체** |
| 4 | 원문 어디에도 없고 짚은 줄도 없다 | 아무것도. 버린 조각으로 센다 |

3 은 모델이 글자를 바꾼 경우다(오타를 고치거나 조사를 붙이는 식). 조각을 버리지 않고 줄
전체를 남기는 것은 2026-09-10 결정이다 — 내용이 빠지는 것보다 소제목이 섞이는 편이 낫다.

"있다" 는 공백·글머리표·구두점과 대소문자를 빼고 본다 (`app/classify/grounding.py` 의
`loose`). 모델이 `•` 를 `-` 로 적은 것은 글자를 바꾼 것이 아니다. 찾는 것만 느슨하고
저장하는 글자는 원문 구간 그대로다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.classify.grounding import loose_with_positions
from app.classify.schema import Piece

# 번호를 붙인 글의 한 줄 앞머리. 모델이 조각이나 근거 문장에 번호까지 옮겨 오면 떼어 낸다
_LINE_MARK = re.compile(r"^\s*\[\d+\]\s*")

# 느슨한 모양으로 이보다 짧은 조각은 어느 줄에서 찾았다고 말할 수 없다. `1` 이나 `가` 는
# 원문 어디에나 있다
MIN_LENGTH = 2


@dataclass(frozen=True)
class Resolved:
    """한 칸의 조각을 원문에서 옮긴 결과."""

    value: str
    # 모델이 글자를 바꿔서 짚은 줄 전체를 대신 남긴 조각 수
    whole_lines: int = 0
    # 짚은 줄도 없고 원문 어디서도 찾지 못해 버린 조각 수
    lost: int = 0


def number_lines(title: str, body: str) -> list[str]:
    """번호를 매길 줄. 0 번이 제목이고, 본문은 빈 줄을 빼고 1 번부터다."""
    return [title.strip(), *(line.strip() for line in body.splitlines() if line.strip())]


def render(lines: Sequence[str], start: int = 0) -> str:
    """줄마다 `[번호]` 를 붙인 글. `start` 는 첫 줄의 번호다."""
    return "\n".join(f"[{start + offset}] {line}" for offset, line in enumerate(lines))


def render_numbers(lines: Sequence[str], numbers: Iterable[int]) -> str:
    """고른 줄만 원래 번호를 붙여 적는다. 긴 공고에서 한 직무의 줄만 보낼 때 쓴다."""
    return "\n".join(f"[{number}] {lines[number]}" for number in numbers)


def to_ranges(numbers: Iterable[int]) -> list[list[int]]:
    """줄 번호들을 이어진 범위로 묶는다. `[1, 2, 3, 7]` 은 `[[1, 3], [7, 7]]` 이다."""
    ranges: list[list[int]] = []
    for number in sorted(set(numbers)):
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return ranges


def from_ranges(ranges: Iterable[Sequence[int]]) -> list[int]:
    """`to_ranges` 의 반대. 이어진 범위를 줄 번호로 푼다."""
    return sorted({number for start, end in ranges for number in range(start, end + 1)})


def strip_line_marks(text: str) -> str:
    """줄마다 앞에 붙은 `[번호]` 를 뗀다. 모델이 번호까지 옮겨 오는 일이 있다."""
    return "\n".join(_LINE_MARK.sub("", line) for line in text.splitlines())


def resolve(pieces: Sequence[Piece], lines: Sequence[str]) -> Resolved:
    """조각을 원문에서 옮겨 한 칸의 값으로 잇는다. 같은 글자는 한 번만 남긴다."""
    stored: list[str] = []
    whole_lines = 0
    lost = 0
    for line_no, raw in pieces:
        text = strip_line_marks(raw).strip()
        if not loose_with_positions(text)[0]:
            # 글머리표나 공백뿐이다. 옮길 내용이 없다
            continue
        pointed = lines[line_no] if 0 <= line_no < len(lines) else ""
        found = _find(text, pointed) or _find_anywhere(text, lines)
        if found is None and pointed:
            # 원문 어디에도 그 글자가 그대로는 없다. 모델이 글자를 바꾼 것이라, 내용이 빠지는
            # 대신 짚은 줄 전체를 남긴다
            found = pointed
            whole_lines += 1
        if found is None:
            lost += 1
            continue
        if found not in stored:
            stored.append(found)
    return Resolved(value="\n".join(stored), whole_lines=whole_lines, lost=lost)


def _find(text: str, line: str) -> str | None:
    """`line` 안에서 `text` 를 느슨하게 찾아 원문 구간을 돌려준다. 없으면 None 이다."""
    needle, _ = loose_with_positions(text)
    if len(needle) < MIN_LENGTH or not line:
        return None
    haystack, positions = loose_with_positions(line)
    start = haystack.find(needle)
    if start < 0:
        return None
    last = start + len(needle) - 1
    begin, end = _widen(line, positions[start], positions[last], text)
    return line[begin : end + 1]


# 잘라 온 구간 앞뒤로 되붙일 수 있는 문장부호. 글머리표(`-`, `•`)는 칸의 내용이 아니라 넣지 않는다
_LEADING_MARKS = frozenset("([{<\"'")
_TRAILING_MARKS = frozenset(")]}>.!?\"'")
_CLOSING_OF = {"(": ")", "[": "]", "{": "}", "<": ">"}
_OPENING_OF = {closing: opening for opening, closing in _CLOSING_OF.items()}


def _widen(line: str, begin: int, end: int, text: str) -> tuple[int, int]:
    """느슨하게 찾은 구간을 문장부호까지 넓힌다.

    느슨한 비교는 문장부호를 걷어내고 맞추므로, 찾은 구간은 첫 글자와 끝 글자에서 멈춘다.
    그대로 자르면 `본사(서울 63빌딩)` 가 `본사(서울 63빌딩` 이 되고 `(필수) 자격증` 이 `필수)
    자격증` 이 된다 (2026-09-13 실제 호출에서 확인).

    두 가지를 되붙인다. 모델이 적은 글의 앞뒤에 있는 괄호·따옴표·마침표가 원문 바로 옆에도 있으면
    그만큼, 그리고 구간 안에서 짝이 맞지 않는 괄호의 짝이 원문 바로 옆에 있으면 그것까지다.
    """
    head = _edge(text, _LEADING_MARKS)
    tail = _edge(text[::-1], _TRAILING_MARKS)
    while head and begin > 0 and line[begin - 1] == head[-1]:
        begin -= 1
        head = head[:-1]
    while tail and end + 1 < len(line) and line[end + 1] == tail[-1]:
        end += 1
        tail = tail[:-1]

    while end + 1 < len(line) and line[end + 1] in _OPENING_OF:
        closing = line[end + 1]
        piece = line[begin : end + 1]
        if piece.count(_OPENING_OF[closing]) <= piece.count(closing):
            break
        end += 1
    while begin > 0 and line[begin - 1] in _CLOSING_OF:
        opening = line[begin - 1]
        piece = line[begin : end + 1]
        if piece.count(_CLOSING_OF[opening]) <= piece.count(opening):
            break
        begin -= 1
    return begin, end


def _edge(text: str, marks: frozenset[str]) -> str:
    """글 앞에 붙은 문장부호들. 뒤쪽을 보려면 뒤집어서 넘기고, 돌려받은 것도 뒤집힌 순서다."""
    taken = []
    for char in text:
        if char not in marks:
            break
        taken.append(char)
    return "".join(taken)


def _find_anywhere(text: str, lines: Sequence[str]) -> str | None:
    """번호가 틀렸을 때. 앞 줄부터 보고 처음 찾은 원문 구간을 돌려준다."""
    for line in lines:
        found = _find(text, line)
        if found is not None:
            return found
    return None
