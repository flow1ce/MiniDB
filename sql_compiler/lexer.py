import re
from .token import Token, KEYWORDS
from common.errors import LexicalError

class Lexer:
    """词法分析器：将原始 SQL 字符串切分为带位置的 Token 序列。"""
    ops = ('<=','>=','!=','==','<>','||','+','-','*','/','%','=','<','>','(',')',',',';','.')
    def __init__(self, text: str): self.text=text; self.i=0; self.line=1; self.col=1
    def _advance(self, s):
        # 消费一段原文，并同步维护字符下标、行号和列号。
        for c in s:
            if c=='\n': self.line+=1; self.col=1
            else: self.col+=1
        self.i += len(s)
    def tokens(self):
        """依次识别空白、注释、字符串、数字、标识符和运算符，末尾追加 EOF。"""
        out=[]
        while self.i < len(self.text):
            c=self.text[self.i]
            if c.isspace(): self._advance(c); continue
            line,col=self.line,self.col
            if self.text.startswith('--',self.i):
                # 跳过 -- 单行注释。
                j=self.text.find('\n',self.i); self._advance(self.text[self.i:] if j<0 else self.text[self.i:j]); continue
            if self.text.startswith('/*',self.i):
                # 跳过 /* ... */ 块注释；未闭合时报告词法错误。
                j=self.text.find('*/',self.i+2)
                if j<0: raise LexicalError('unterminated comment',line,col)
                self._advance(self.text[self.i:j+2]); continue
            if c=="'":
                # 读取字符串字面量；SQL 中连续两个单引号表示一个单引号。
                j=self.i+1; val=''
                while j<len(self.text):
                    if self.text[j]=="'":
                        if j+1<len(self.text) and self.text[j+1]=="'": val+="'"; j+=2; continue
                        break
                    val+=self.text[j]; j+=1
                if j>=len(self.text): raise LexicalError('unterminated string literal',line,col)
                raw=self.text[self.i:j+1]; self._advance(raw); out.append(Token('STRING',val,line,col)); continue
            m=re.match(r'(?:\d+\.\d+|\d+)',self.text[self.i:])
            if m:
                raw=m.group(0); self._advance(raw); out.append(Token('NUMBER',raw,line,col)); continue
            m=re.match(r'[A-Za-z_][A-Za-z0-9_]*',self.text[self.i:])
            if m:
                raw=m.group(0); self._advance(raw); up=raw.upper(); out.append(Token(up if up in KEYWORDS else 'IDENT',raw,line,col)); continue
            op=next((x for x in self.ops if self.text.startswith(x,self.i)),None)
            if op:
                self._advance(op); out.append(Token(op,op,line,col)); continue
            raise LexicalError(f"illegal character {c!r}",line,col)
        out.append(Token('EOF','',self.line,self.col)); return out
__all__ = ['Lexer']
