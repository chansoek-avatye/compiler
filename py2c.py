"""
py2c.py — Python 코드를 C 코드로 번역하는 아주 작은 컴파일러

    입력 (hello.py):   print("Hello, world!")
    출력 (hello.c):    #include <stdio.h>
                       int main() {
                           printf("Hello, world!\\n");
                           return 0;
                       }

지원하는 Python (부분집합):
    print("문자열")
    print(1 + 2 * 3)
    x = 10            정수 변수
    print("x는", x)   여러 값 출력 (Python처럼 공백으로 구분)
    # 주석

실행:
    python py2c.py hello.py          → hello.c 생성 → gcc 컴파일 → 실행
    python py2c.py hello.py --dump   → 토큰 / AST / C 코드 단계별 출력
"""

import subprocess
import sys
from dataclasses import dataclass


# ═════════════════════════════════════════════
# 1단계: Lexer — Python 소스 글자들을 토큰으로 자른다
# ═════════════════════════════════════════════

@dataclass
class Token:
    type: str     # NUMBER, STRING, IDENT, PRINT, PLUS, NEWLINE ...
    value: object
    line: int


SYMBOLS = {
    "+": "PLUS", "-": "MINUS", "*": "STAR", "/": "SLASH",
    "=": "ASSIGN", "(": "LPAREN", ")": "RPAREN", ",": "COMMA",
}


def tokenize(src: str) -> list[Token]:
    tokens = []
    i, line = 0, 1

    while i < len(src):
        ch = src[i]

        # Python은 "줄바꿈"이 문장의 끝이다. (C의 ';' 역할)
        if ch == "\n":
            if tokens and tokens[-1].type != "NEWLINE":
                tokens.append(Token("NEWLINE", None, line))
            line += 1
            i += 1

        # 줄 맨 앞의 공백 = 들여쓰기 → 이 버전에서는 아직 지원하지 않음
        elif ch == " " and (i == 0 or src[i - 1] == "\n") and src[i:].lstrip(" ")[:1] not in ("\n", "#", ""):
            raise SyntaxError(f"{line}번째 줄: 들여쓰기는 아직 지원하지 않습니다")

        elif ch in " \t\r":
            i += 1

        elif ch == "#":                              # 주석: 줄 끝까지 버림
            while i < len(src) and src[i] != "\n":
                i += 1

        elif ch.isdigit():                           # 정수
            start = i
            while i < len(src) and src[i].isdigit():
                i += 1
            tokens.append(Token("NUMBER", int(src[start:i]), line))

        elif ch in "\"'":                            # 문자열: 같은 따옴표가 나올 때까지
            quote = ch
            i += 1
            chars = []
            while i < len(src) and src[i] != quote:
                if src[i] == "\n":
                    raise SyntaxError(f"{line}번째 줄: 문자열이 닫히지 않았습니다")
                if src[i] == "\\" and i + 1 < len(src):    # \n, \", \\ 같은 이스케이프
                    chars.append({"n": "\n", "t": "\t"}.get(src[i + 1], src[i + 1]))
                    i += 2
                else:
                    chars.append(src[i])
                    i += 1
            if i >= len(src):
                raise SyntaxError(f"{line}번째 줄: 문자열이 닫히지 않았습니다")
            i += 1                                   # 닫는 따옴표
            tokens.append(Token("STRING", "".join(chars), line))

        elif ch.isalpha() or ch == "_":              # 이름: print는 키워드로 분류
            start = i
            while i < len(src) and (src[i].isalnum() or src[i] == "_"):
                i += 1
            word = src[start:i]
            tokens.append(Token("PRINT" if word == "print" else "IDENT", word, line))

        elif ch in SYMBOLS:
            tokens.append(Token(SYMBOLS[ch], ch, line))
            i += 1

        else:
            raise SyntaxError(f"{line}번째 줄: 알 수 없는 문자 '{ch}'")

    if tokens and tokens[-1].type != "NEWLINE":
        tokens.append(Token("NEWLINE", None, line))
    tokens.append(Token("EOF", None, line))
    return tokens


# ═════════════════════════════════════════════
# 2단계: Parser — 토큰을 AST(트리)로 묶는다
# ═════════════════════════════════════════════
# 문법:
#   program   → statement*
#   statement → "print" "(" [arg ("," arg)*] ")" NEWLINE
#             | IDENT "=" expr NEWLINE
#   arg       → STRING | expr
#   expr      → term (("+" | "-") term)*
#   term      → unary (("*" | "/") unary)*
#   unary     → "-" unary | primary
#   primary   → NUMBER | IDENT | "(" expr ")"

@dataclass
class Num:    value: int
@dataclass
class Str:    value: str
@dataclass
class Var:    name: str; line: int
@dataclass
class Neg:    operand: object
@dataclass
class BinOp:  op: str; left: object; right: object
@dataclass
class Print:  args: list
@dataclass
class Assign: name: str; value: object; line: int


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def cur(self):
        return self.tokens[self.pos]

    def check(self, *types):
        return self.cur().type in types

    def advance(self):
        tok = self.cur()
        self.pos += 1
        return tok

    def expect(self, type_, what):
        if not self.check(type_):
            tok = self.cur()
            got = "줄 끝" if tok.type == "NEWLINE" else (tok.value or "파일 끝")
            raise SyntaxError(f"{tok.line}번째 줄: {what}이(가) 필요한데 '{got}'이(가) 왔습니다")
        return self.advance()

    def parse(self):
        stmts = []
        while not self.check("EOF"):
            stmts.append(self.statement())
        return stmts

    def statement(self):
        if self.check("PRINT"):
            self.advance()
            self.expect("LPAREN", "'('")
            args = []
            if not self.check("RPAREN"):
                args.append(self.arg())
                while self.check("COMMA"):
                    self.advance()
                    args.append(self.arg())
            self.expect("RPAREN", "')'")
            self.expect("NEWLINE", "줄바꿈")
            return Print(args)

        if self.check("IDENT"):
            tok = self.advance()
            self.expect("ASSIGN", "'='")
            value = self.expr()
            self.expect("NEWLINE", "줄바꿈")
            return Assign(tok.value, value, tok.line)

        tok = self.cur()
        raise SyntaxError(f"{tok.line}번째 줄: 문장을 시작할 수 없는 '{tok.value}'")

    def arg(self):
        if self.check("STRING"):
            return Str(self.advance().value)
        return self.expr()

    def expr(self):
        node = self.term()
        while self.check("PLUS", "MINUS"):
            op = self.advance().value
            node = BinOp(op, node, self.term())
        return node

    def term(self):
        node = self.unary()
        while self.check("STAR", "SLASH"):
            op = self.advance().value
            node = BinOp(op, node, self.unary())
        return node

    def unary(self):
        if self.check("MINUS"):
            self.advance()
            return Neg(self.unary())
        return self.primary()

    def primary(self):
        if self.check("NUMBER"):
            return Num(self.advance().value)
        if self.check("IDENT"):
            tok = self.advance()
            return Var(tok.value, tok.line)
        if self.check("LPAREN"):
            self.advance()
            node = self.expr()
            self.expect("RPAREN", "')'")
            return node
        if self.check("STRING"):
            raise SyntaxError(f"{self.cur().line}번째 줄: 문자열 계산은 아직 지원하지 않습니다")
        tok = self.cur()
        raise SyntaxError(f"{tok.line}번째 줄: 값이 와야 하는데 '{tok.value}'이(가) 왔습니다")


# ═════════════════════════════════════════════
# 3단계: Code Generator — AST를 C 코드로 번역한다
# ═════════════════════════════════════════════

def c_string_literal(text: str) -> str:
    """Python 문자열을 C의 printf 형식 문자열 안에 안전하게 넣는다."""
    return (text.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\t", "\\t")
                .replace("%", "%%"))      # printf에서 %는 특수문자라 %%로


class CGenerator:
    def __init__(self):
        self.declared = set()   # Python은 선언이 없지만 C는 있어야 함 → 첫 대입 때 선언
        self.body = []

    def generate(self, stmts) -> str:
        for s in stmts:
            self.statement(s)
        lines = ["#include <stdio.h>", "", "int main() {"]
        lines += ["    " + line for line in self.body]
        lines += ["    return 0;", "}", ""]
        return "\n".join(lines)

    def statement(self, node):
        if isinstance(node, Print):
            # print("x는", x, 1+2)  →  printf("x는 %ld %ld\n", x, (1 + 2));
            fmt_parts, values = [], []
            for a in node.args:
                if isinstance(a, Str):
                    fmt_parts.append(c_string_literal(a.value))
                else:
                    fmt_parts.append("%ld")
                    values.append(self.expr(a))
            fmt = " ".join(fmt_parts) + "\\n"        # Python print: 공백 구분 + 줄바꿈
            args = "".join(f", {v}" for v in values)
            self.body.append(f'printf("{fmt}"{args});')

        elif isinstance(node, Assign):
            value = self.expr(node.value)
            if node.name in self.declared:
                self.body.append(f"{node.name} = {value};")
            else:
                self.declared.add(node.name)
                self.body.append(f"long {node.name} = {value};")

    def expr(self, node) -> str:
        if isinstance(node, Num):
            return str(node.value)
        if isinstance(node, Var):
            if node.name not in self.declared:
                raise NameError(f"{node.line}번째 줄: name '{node.name}' is not defined")
            return node.name
        if isinstance(node, Neg):
            return f"(-{self.expr(node.operand)})"
        if isinstance(node, BinOp):
            return f"({self.expr(node.left)} {node.op} {self.expr(node.right)})"
        raise TypeError(node)


# ═════════════════════════════════════════════
# 실행: .py → .c → (gcc) → 실행 파일 → 실행
# ═════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("사용법: python py2c.py <파일.py> [--dump]")
        sys.exit(1)

    py_path = sys.argv[1]
    dump = "--dump" in sys.argv
    with open(py_path, encoding="utf-8") as f:
        source = f.read()

    try:
        tokens = tokenize(source)
        ast = Parser(tokens).parse()
        c_code = CGenerator().generate(ast)
    except (SyntaxError, NameError) as e:
        print(f"컴파일 에러: {e}")
        sys.exit(1)

    base = py_path.rsplit(".", 1)[0]
    c_path, exe_path = base + ".c", base + ".out"
    with open(c_path, "w", encoding="utf-8") as f:
        f.write(c_code)

    if dump:
        print("=== 1. 토큰 ===")
        for t in tokens:
            print(f"  {t.line:>3}  {t.type:<8} {t.value!r}")
        print("\n=== 2. AST ===")
        for s in ast:
            print(" ", s)
        print()
    print(f"=== 생성된 C 코드 ({c_path}) ===")
    print(c_code)

    result = subprocess.run(["gcc", c_path, "-o", exe_path], capture_output=True, text=True)
    if result.returncode != 0:
        print("gcc 에러:\n" + result.stderr)
        sys.exit(1)

    print("=== 실행 결과 ===")
    subprocess.run([exe_path if "/" in exe_path else "./" + exe_path])


if __name__ == "__main__":
    main()