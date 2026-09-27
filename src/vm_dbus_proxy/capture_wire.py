"""D-Bus value codec used only by the ScreenCast adapter."""
import struct
from .protocol import ProtocolError, signature_types

ALIGN = dict(zip('ybnqiuxtdhsogv', (1,4,2,2,4,4,8,8,8,4,4,4,1,1)))
ALIGN.update({'a':4, '(':8, '{':8})

def tag(kind):
    return kind if isinstance(kind, str) else kind[0]

class Reader:
    def __init__(self, data, endian='<'):
        self.data, self.endian, self.pos = data, endian, 0
    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise ProtocolError('truncated capture D-Bus value')
        out = self.data[self.pos:self.pos+n]; self.pos += n
        return out
    def align(self, n):
        if any(self.take(-self.pos % n)):
            raise ProtocolError('nonzero padding')
    def number(self, fmt):
        return struct.unpack(self.endian+fmt, self.take(struct.calcsize(fmt)))[0]
    def value(self, kind, depth=0):
        if depth > 32: raise ProtocolError('value too deeply nested')
        t = tag(kind); self.align(ALIGN[t])
        if t in 'ybnqiuxtdh':
            return self.number(dict(y='B',b='I',n='h',q='H',i='i',u='I',x='q',t='Q',d='d',h='I')[t])
        if t in 'sog':
            n = self.number('B' if t == 'g' else 'I'); raw = self.take(n)
            if self.take(1) != b'\0' or b'\0' in raw: raise ProtocolError('bad string')
            return raw.decode('utf-8')
        if t == 'v':
            sig = self.value('g', depth+1); types = signature_types(sig)
            if len(types)!=1: raise ProtocolError('bad variant')
            return (sig, self.value(types[0], depth+1))
        if t in '({': return tuple(self.value(k, depth+1) for k in kind[1])
        n = self.number('I'); self.align(ALIGN[tag(kind[1])]); end=self.pos+n
        if end > len(self.data): raise ProtocolError('array exceeds message')
        out=[]
        while self.pos<end: out.append(self.value(kind[1],depth+1))
        if self.pos!=end: raise ProtocolError('array size mismatch')
        return out

class Writer:
    def __init__(self, endian='<'):
        self.data=bytearray(); self.endian=endian
    def align(self,n): self.data.extend(b'\0'*(-len(self.data)%n))
    def number(self,f,v): self.data.extend(struct.pack(self.endian+f,v))
    def value(self,kind,v):
        t=tag(kind); self.align(ALIGN[t])
        if t in 'ybnqiuxtdh': self.number(dict(y='B',b='I',n='h',q='H',i='i',u='I',x='q',t='Q',d='d',h='I')[t],v)
        elif t in 'sog':
            raw=v.encode(); self.number('B' if t=='g' else 'I',len(raw)); self.data.extend(raw+b'\0')
        elif t=='v':
            self.value('g',v[0]); self.value(signature_types(v[0])[0],v[1])
        elif t in '({':
            for k,x in zip(kind[1],v): self.value(k,x)
        else:
            pos=len(self.data); self.number('I',0); self.align(ALIGN[tag(kind[1])]); start=len(self.data)
            for x in v: self.value(kind[1],x)
            self.data[pos:pos+4]=struct.pack(self.endian+'I',len(self.data)-start)

def body(frame):
    h=struct.unpack(frame.endian+'I',frame.data[12:16])[0]
    reader=Reader(frame.data[16+((h+7)&~7):],frame.endian)
    result=tuple(reader.value(k) for k in signature_types(frame.fields.get(8,'')))
    if reader.pos!=len(reader.data): raise ProtocolError('trailing body data')
    return result

def message(message_type, serial, fields, values=(), flags=0, endian='<'):
    b=Writer(endian)
    kinds=signature_types(fields.get(8,''))
    if len(kinds)!=len(values): raise ProtocolError('body arity mismatch')
    for k,v in zip(kinds,values): b.value(k,v)
    h=Writer(endian)
    for code,value in fields.items():
        if code not in range(1,10): raise ProtocolError('unsupported rewritten header')
        h.align(8); h.value('y',code)
        sig={1:'o',2:'s',3:'s',4:'s',5:'u',6:'s',7:'s',8:'g',9:'u'}[code]
        h.value('v',(sig,value))
    n=len(h.data); h.align(8)
    return (b'l' if endian=='<' else b'B')+bytes((message_type,flags,1))+struct.pack(endian+'III',len(b.data),serial,n)+h.data+b.data

def replace_body(frame, values):
    return message(frame.message_type,frame.serial,frame.fields,values,frame.flags,frame.endian)

def replace_fields(frame,fields):
    """Rewrite headers without decoding arbitrary application bodies."""
    h=Writer(frame.endian)
    for code,value in fields.items():
        if code not in range(1,10): raise ProtocolError('unsupported rewritten header')
        h.align(8); h.value('y',code)
        h.value('v',({1:'o',2:'s',3:'s',4:'s',5:'u',6:'s',7:'s',8:'g',9:'u'}[code],value))
    size=len(h.data); h.align(8)
    original=struct.unpack(frame.endian+'I',frame.data[12:16])[0]
    b=frame.data[16+((original+7)&~7):]
    return frame.data[:12]+struct.pack(frame.endian+'I',size)+h.data+b

def replace_reply_serial(frame, serial):
    # Retain unknown future header variants and opaque application bodies.
    from .protocol import HeaderReader
    size=struct.unpack(frame.endian+'I',frame.data[12:16])[0]
    r=HeaderReader(frame.data[:16+size],frame.endian)
    while r.pos<len(r.data):
        r.align(8); code=r.number('B'); sig=r.value('g')
        if code==5:
            r.align(4); data=bytearray(frame.data)
            data[r.pos:r.pos+4]=struct.pack(frame.endian+'I',serial)
            return bytes(data)
        r.value(signature_types(sig)[0])
    raise ProtocolError('reply serial absent')
