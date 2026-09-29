"""ScreenCast-aware D-Bus tunnel; descriptors stay in their kernel domain.

The host negotiates UNIX_FD with the existing filtering proxy. Only the reply
of an internally issued OpenPipeWireRemote for a successful Start may feed
capture. Everything else follows the ordinary no-FD policy.
"""
import argparse
import array
import errno
import json
import hashlib
import logging
import os
import select
import socket
import struct
import threading
import time
from . import protocol
from .capture_wire import body as decode_body, message, replace_body, replace_fields, replace_reply_serial
from .proxy import auth_line, raise_fd_limit

LOG=logging.getLogger('vm-dbus-capture')
PORTAL='org.freedesktop.portal.Desktop'
SCREEN='org.freedesktop.portal.ScreenCast'
BUS='org.freedesktop.DBus'
BUS_PATH='/org/freedesktop/DBus'
PATH='/org/freedesktop/portal/desktop'
LIMIT=protocol.MAX_MESSAGE_BYTES
MAX_SESSIONS=4
MAX_REQUESTS=64


def body(frame):
    # Only control messages are decoded; opaque application traffic retains the
    # general frame limit. Bound allocation from nested variant containers.
    if len(frame.data)>65536:
        raise protocol.ProtocolError('capture control message exceeds 64 KiB')
    return decode_body(frame)

class MemorySocket:
    def __init__(self,data): self.data=data
    def recvmsg(self,n,*args):
        out,self.data=self.data[:n],self.data[n:]
        return out,[],0,None

def frame(data):
    s=MemorySocket(data); f=protocol.read_frame(s)
    if f is None or s.data: raise protocol.ProtocolError('invalid tunnel frame')
    return f

def receive_fds(sock):
    """Retain duplicates while the normal bounded parser closes received FDs."""
    fds=[]
    class Retain:
        def recvmsg(self,*args):
            data,anc,flags,address=sock.recvmsg(*args)
            for level,kind,payload in anc:
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                    a=array.array('i'); a.frombytes(payload[:len(payload)//a.itemsize*a.itemsize])
                    for fd in a: fds.append(os.dup(fd))
            return data,anc,flags,address
    try:
        f=protocol.read_frame(Retain())
        return f,fds
    except BaseException:
        for fd in fds: os.close(fd)
        raise

def record(payload):
    """A JSON tunnel record naming a session; the peer end is untrusted."""
    value=json.loads(payload)
    if not isinstance(value,dict) or not isinstance(value.get('session'),str):
        raise protocol.ProtocolError('invalid tunnel record')
    return value

def exact(sock,n):
    out=bytearray()
    while len(out)<n:
        part=sock.recv(n-len(out))
        if not part: raise EOFError()
        out.extend(part)
    return bytes(out)

class Tunnel:
    def __init__(self,sock): self.sock=sock; self.lock=threading.Lock()
    def send(self,kind,payload=b''):
        if len(payload)>LIMIT: raise protocol.ProtocolError('oversized tunnel record')
        with self.lock: self.sock.sendall(kind+struct.pack('!I',len(payload))+payload)
    def recv(self):
        head=exact(self.sock,5); n=struct.unpack('!I',head[1:])[0]
        if n>LIMIT: raise protocol.ProtocolError('oversized tunnel record')
        return head[:1],exact(self.sock,n)
    def json(self,kind,value): self.send(kind,json.dumps(value,separators=(',',':')).encode())


def broker(path,role,session,channel,pipewire=None,node=None):
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET); sock.settimeout(20)
    try:
        sock.connect(path)
        fields={'role':role,'session':hashlib.sha256(session.encode()).hexdigest()}
        fds=[channel.fileno()]
        if role=='host': fields['node']=node; fds=[pipewire,channel.fileno()]
        sock.sendmsg([json.dumps(fields).encode()],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',fds))])
        data,anc,flags,_=sock.recvmsg(4096,socket.CMSG_SPACE(16),getattr(socket,'MSG_CMSG_CLOEXEC',0))
        received=[]
        for level,kind,payload in anc:
            if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                a=array.array('i'); a.frombytes(payload[:len(payload)//a.itemsize*a.itemsize]); received.extend(a)
        try:
            if flags & (socket.MSG_TRUNC|socket.MSG_CTRUNC): raise protocol.ProtocolError('truncated broker reply')
            answer=json.loads(data)
            if role=='host':
                if answer.get('ok') is not True or received: raise protocol.ProtocolError('capture host broker refused')
                sock.settimeout(None); return sock,None,None
            if len(received)!=1 or type(answer.get('node')) is not int or not 0<answer['node']<2**32:
                raise protocol.ProtocolError('capture guest broker refused')
            sock.settimeout(None); return sock,received.pop(),answer['node']
        finally:
            for fd in received: os.close(fd)
    except BaseException: sock.close(); raise

class Session:
    def __init__(self,key):
        self.key=key; self.channel,self.peer=socket.socketpair(); self.control=None; self.pw=None; self.closed=False
    def close(self):
        if self.closed: return
        self.closed=True
        if self.control: self.control.close()
        if self.pw is not None: os.close(self.pw); self.pw=None
        for s in (self.channel,self.peer):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            s.close()

class Adapter:
    def __init__(self,tunnel,broker_path):
        self.tunnel=tunnel; self.broker_path=broker_path; self.sessions={}; self.lock=threading.RLock(); self.closed=False
    def new_session(self,key):
        if not isinstance(key,str) or not key.startswith('/org/freedesktop/portal/desktop/session/') or len(key)>1024:
            raise protocol.ProtocolError('invalid capture session')
        with self.lock:
            if self.closed or key in self.sessions or len(self.sessions)>=MAX_SESSIONS: raise protocol.ProtocolError('capture session limit')
            session=Session(key); self.sessions[key]=session; return session
    def pump(self,session):
        try:
            while True:
                data=session.channel.recv(65536)
                if not data: break
                key=session.key.encode(); self.tunnel.send(b'M',struct.pack('!H',len(key))+key+data)
        except (OSError,EOFError): pass
        finally:
            try: self.tunnel.json(b'X',{'session':session.key})
            except OSError: pass
            self.close_session(session.key)
    def media(self,payload):
        if len(payload)<2: raise protocol.ProtocolError('short media record')
        n=struct.unpack('!H',payload[:2])[0]
        if n>1024 or len(payload)<2+n or len(payload)>65536+1026: raise protocol.ProtocolError('invalid media record')
        key=payload[2:2+n].decode()
        with self.lock: s=self.sessions.get(key)
        if s:
            data=memoryview(payload)[2+n:]
            deadline=time.monotonic()+10
            while data:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([], [s.channel], [], remaining)[1]:
                    raise TimeoutError('capture media receiver stalled')
                try: data=data[s.channel.send(data,socket.MSG_DONTWAIT):]
                except BlockingIOError: continue
    def close_session(self,key):
        with self.lock: s=self.sessions.pop(key,None)
        if s: s.close()
    def close(self):
        with self.lock:
            self.closed=True
            keys=list(self.sessions)
        for key in keys: self.close_session(key)


def guest_auth(client,tunnel):
    client.settimeout(10)
    if exact(client,1)!=b'\0': raise protocol.ProtocolError('missing auth NUL')
    tunnel.send(b'A',b'\0')
    authenticated=False; negotiated=False
    deadline=time.monotonic()+10
    while True:
        line=auth_line(client,deadline)
        if line==b'NEGOTIATE_UNIX_FD\r\n':
            if not authenticated: raise protocol.ProtocolError('premature FD negotiation')
            client.sendall(b'AGREE_UNIX_FD\r\n'); negotiated=True; continue
        tunnel.send(b'A',line)
        kind,response=tunnel.recv()
        if line==b'BEGIN\r\n':
            if not authenticated or kind!=b'R': raise protocol.ProtocolError('invalid BEGIN')
            client.settimeout(None); return negotiated
        if kind!=b'A': raise protocol.ProtocolError('invalid auth record')
        authenticated=response.startswith(b'OK ')
        client.sendall(response)

def host_auth(tunnel,upstream):
    upstream.settimeout(10); deadline=time.monotonic()+10
    if tunnel.recv()!=(b'A',b'\0'): raise protocol.ProtocolError('missing auth NUL')
    upstream.sendall(b'\0'); authenticated=False
    while True:
        kind,line=tunnel.recv()
        if kind!=b'A' or len(line)>16384 or not line.endswith(b'\r\n'): raise protocol.ProtocolError('invalid auth record')
        if line==b'BEGIN\r\n':
            if not authenticated: raise protocol.ProtocolError('invalid BEGIN')
            upstream.sendall(b'NEGOTIATE_UNIX_FD\r\n')
            if auth_line(upstream,deadline)!=b'AGREE_UNIX_FD\r\n': raise protocol.ProtocolError('host UNIX_FD unavailable')
            upstream.sendall(line); upstream.settimeout(None); tunnel.send(b'R'); return
        if line.startswith(b'NEGOTIATE_UNIX_FD'): raise protocol.ProtocolError('guest cannot negotiate host FDs')
        upstream.sendall(line); response=auth_line(upstream,deadline)
        authenticated=response.startswith(b'OK '); tunnel.send(b'A',response)

class Host(Adapter):
    def __init__(self,tunnel,broker_path,upstream):
        super().__init__(tunnel,broker_path); self.upstream=upstream; self.write_lock=threading.Lock(); self.serial=1
        self.pending={}; self.requests={}; self.held={}; self.timers={}; self.portal_sender=None
    def classify(self,f):
        """What a reply to this call will need. Keep that, never the call:
        up to 4096 calls may be outstanding, each up to the frame limit."""
        dest,path,iface,member,sig=(f.fields.get(k) for k in (6,1,2,3,8))
        small=len(f.data)<=65536
        if (dest==BUS and path==BUS_PATH and iface==BUS and member=='GetNameOwner'
                and sig=='s' and small and body(f)==(PORTAL,)):
            return 'owner',None
        if (dest is not None and dest in (PORTAL,self.portal_sender) and path==PATH and iface==SCREEN
                and member=='Start' and sig=='osa{sv}' and small):
            key=body(f)[0]
            if len(key)<=1024: return 'start',key
        return None,None
    def send_upstream(self,f,internal=None):
        with self.write_lock:
            serial=self.serial; self.serial=self.serial%0xffffffff+1
            if len(self.pending)>=4096: raise protocol.ProtocolError('too many outstanding calls')
            if f.message_type==1 and not f.flags&1:
                kind,arg=self.classify(f)
                self.pending[serial]=(f.serial,kind,arg,f.fields.get(6)==PORTAL,internal)
            # Preserve the opaque body and just change the fixed-header serial.
            data=bytearray(f.data); data[8:12]=struct.pack(f.endian+'I',serial); self.upstream.sendall(data)
    def from_guest(self,data):
        f=frame(data)
        if f.fd_count: raise protocol.ProtocolError('FD in tunnel')
        if f.message_type==1 and f.fields.get(2)=='org.freedesktop.portal.Session' and f.fields.get(3)=='Close':
            self.close_session(f.fields.get(1))
        if f.message_type==1 and f.fields.get(6) in (PORTAL,self.portal_sender) and f.fields.get(1)==PATH and f.fields.get(2)==SCREEN and f.fields.get(3)=='SelectSources' and f.fields.get(8)=='oa{sv}':
            key,options=body(f)
            opts=dict(options)
            # The bridge publishes one source with cursor pixels embedded.
            opts['multiple']=('b',0)
            if opts.get('cursor_mode',('u',1))[1]==4: opts['cursor_mode']=('u',2)
            # Every VM share goes through the host picker: no restore tokens.
            opts['persist_mode']=('u',0); opts.pop('restore_token',None)
            f=frame(replace_body(f,(key,list(opts.items()))))
        self.send_upstream(f)
    def start_media(self,key,node,pw):
        session=self.new_session(key)
        try:
            self.tunnel.json(b'S',{'session':key,'sender':self.held[key].fields.get(7)})
            threading.Thread(target=self.pump,args=(session,),daemon=True).start()
            session.control,_,_=broker(self.broker_path,'host',key,session.peer,pw,node)
            session.peer.close()
            if session.closed and session.control: session.control.close()
        except BaseException:
            self.close_session(key); raise
    def from_host(self,f,fds):
        try:
            pending=self.pending.pop(f.fields.get(5),None) if f.message_type in (2,3) else None
            if pending:
                serial,kind,arg,to_portal,internal=pending
                # GDBus resolves the well-known name before calling a proxy;
                # all its portal calls may therefore target only :1.NNN.
                # Learn that identity only from the bus daemon's reply.
                if (kind=='owner' and f.message_type==2 and f.fields.get(7)==BUS
                        and f.fields.get(8)=='s'):
                    owner=body(f)[0]
                    if not owner.startswith(':'): raise protocol.ProtocolError('invalid portal owner')
                    self.portal_sender=owner
                elif (to_portal and f.message_type==2
                        and f.fields.get(7,'').startswith(':')):
                    self.portal_sender=f.fields[7]
                if internal:
                    key,node=internal
                    if f.message_type!=2 or f.fields.get(8)!='h' or len(fds)!=1 or body(f)!=(0,):
                        self.fail(key); return
                    # Bootstrap may wait for a publisher; never block D-Bus reads.
                    pw=os.dup(fds[0])
                    def boot():
                        try: self.start_media(key,node,pw)
                        except Exception as exc: LOG.warning('capture start failed: %s',exc); self.fail(key)
                        finally: os.close(pw)
                    threading.Thread(target=boot,daemon=True).start(); return
                if kind=='start' and f.message_type==2 and f.fields.get(8)=='o':
                    request=body(f)[0]
                    while len(self.requests)>=MAX_REQUESTS: self.requests.pop(next(iter(self.requests)))
                    self.requests[request]=(arg,f.fields.get(7))
                    LOG.info('tracking ScreenCast Start response')
                fdata=replace_reply_serial(f,serial)
            else: fdata=f.data
            if fds:
                if f.message_type in (2,3):
                    # Use restored client reply serial, and strip the FD header.
                    fields=dict(f.fields); fields[5]=serial if pending else fields[5]
                    from dataclasses import replace
                    self.tunnel.send(b'D',protocol.make_error(replace(f,fields=fields),f.serial))
                elif f.message_type==1 and not f.flags&1:
                    # A host peer's FD-bearing call: answer it rather than
                    # leaving the caller to time out.
                    self.send_upstream(frame(protocol.make_error(f)))
                return
            if (f.message_type==4 and f.fields.get(7)==BUS and f.fields.get(1)==BUS_PATH
                    and f.fields.get(2)==BUS and f.fields.get(3)=='NameOwnerChanged'
                    and f.fields.get(8)=='sss'):
                name,old,new=body(f)
                if name==PORTAL:
                    if new and not new.startswith(':'): raise protocol.ProtocolError('invalid portal owner')
                    self.portal_sender=new or None
            if f.message_type==4 and f.fields.get(2)=='org.freedesktop.portal.Session' and f.fields.get(3)=='Closed':
                key=f.fields.get(1); self.close_session(key); self.tunnel.json(b'X',{'session':key})
            tracked=self.requests.get(f.fields.get(1)) if (f.message_type==4 and f.fields.get(2)=='org.freedesktop.portal.Request'
                and f.fields.get(3)=='Response' and f.fields.get(8)=='ua{sv}') else None
            # Only the portal that answered Start can approve it; anything
            # else on that path passes through untouched and is not capture.
            if tracked and f.fields.get(7)==tracked[1]:
                del self.requests[f.fields.get(1)]
                key,sender=tracked
                result,options=body(f); streams=dict(options).get('streams')
                if result==0 and streams:
                    if streams[0]!='a(ua{sv})' or len(streams[1])!=1:
                        self.refuse(f,key); return
                    node=streams[1][0][0]
                    LOG.info('approved ScreenCast stream; opening restricted PipeWire remote')
                    with self.lock:
                        refused=self.closed or key in self.held or len(self.held)>=MAX_SESSIONS
                        if not refused:
                            self.held[key]=f
                            timer=threading.Timer(30,lambda: self.fail(key,timeout=True))
                            self.timers[key]=timer
                            timer.daemon=True; timer.start()
                    if refused: self.refuse(f,key); return
                    call=frame(message(1,1,{1:PATH,2:SCREEN,3:'OpenPipeWireRemote',6:PORTAL,8:'oa{sv}'},(key,[])))
                    self.send_upstream(call,(key,node)); return
            self.tunnel.send(b'D',fdata)
        finally:
            for fd in fds: os.close(fd)
    def refuse(self,response,key):
        """An approved share this bridge cannot carry: fail it to the app and
        end the host session, so the host's stream and indicator go away."""
        LOG.warning('approved ScreenCast stream cannot be bridged; closing it')
        self.tunnel.send(b'D',replace_body(response,(2,[])))
        close=frame(message(1,1,{1:key,2:'org.freedesktop.portal.Session',3:'Close',6:PORTAL},flags=1))
        try: self.send_upstream(close)
        except OSError: pass
    def ready(self,payload):
        value=record(payload); key=value['session']; node=value.get('node')
        if type(node)!=int or not 0<node<2**32: raise protocol.ProtocolError('invalid guest node')
        with self.lock:
            f=self.held.pop(key,None)
            timer=self.timers.pop(key,None)
            if timer: timer.cancel()
        if f is None: return
        result,options=body(f); out=[]
        for name,v in options:
            if name=='streams': v=(v[0],[(node,v[1][0][1])])
            out.append((name,v))
        LOG.info('guest capture source ready; returning translated Start response')
        self.tunnel.send(b'D',replace_body(f,(result,out)))
    def fail(self,key,timeout=False):
        with self.lock:
            f=self.held.pop(key,None)
            timer=self.timers.pop(key,None)
            if timer: timer.cancel()
            # A fired timeout must not tear down a session that became ready.
            if self.closed or (timeout and f is None): return
            live=key in self.sessions
        if f: self.tunnel.send(b'D',replace_body(f,(2,[])))
        self.close_session(key)
        if f or live:
            close=frame(message(1,1,{1:key,2:'org.freedesktop.portal.Session',3:'Close',6:PORTAL},flags=1))
            try: self.send_upstream(close)
            except OSError: pass
        self.tunnel.json(b'X',{'session':key})

    def close(self):
        with self.lock:
            self.closed=True
            self.held.clear()
            for timer in self.timers.values(): timer.cancel()
            self.timers.clear()
        super().close()

class Guest(Adapter):
    def __init__(self,tunnel,broker_path,client):
        super().__init__(tunnel,broker_path); self.client=client; self.client_write=threading.Lock(); self.serial=0x80000000
    def deliver(self,data,fd=None):
        with self.client_write:
            if fd is None: self.client.sendall(data)
            else:
                n=self.client.sendmsg([data],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
                if n<len(data): self.client.sendall(data[n:])
    def start(self,payload):
        value=record(payload); key=value['session']; sender=value.get('sender')
        if sender is not None and not isinstance(sender,str): raise protocol.ProtocolError('invalid portal sender')
        session=self.new_session(key); session.sender=sender
        threading.Thread(target=self.pump,args=(session,),daemon=True).start()
        def boot():
            try:
                session.control,session.pw,node=broker(self.broker_path,'guest',key,session.peer)
                session.peer.close()
                if session.closed:
                    if session.control: session.control.close()
                    if session.pw is not None: os.close(session.pw); session.pw=None
                    return
                self.tunnel.json(b'N',{'session':key,'node':node})
            except Exception as exc:
                LOG.warning('guest capture start failed: %s',exc); self.close_session(key); self.tunnel.json(b'X',{'session':key})
        threading.Thread(target=boot,daemon=True).start()
    def from_app(self,f,negotiated):
        if (f.message_type==1 and f.fields.get(1)==PATH and f.fields.get(2)==SCREEN
                and f.fields.get(3)=='OpenPipeWireRemote' and f.fields.get(8)=='oa{sv}'):
            key=body(f)[0]
            with self.lock:
                s=self.sessions.get(key)
                # Duplicate under the lock: close_session may close s.pw.
                pw=os.dup(s.pw) if negotiated and s and s.pw is not None else None
            try:
                if f.fields.get(6)!=PORTAL and (not s or f.fields.get(6)!=s.sender):
                    self.tunnel.send(b'D',f.data); return
                if pw is None:
                    self.deliver(protocol.make_error(f,self.serial)); self.serial+=1; return
                fields={5:f.serial,8:'h',9:1}
                if s.sender: fields[7]=s.sender
                if 7 in f.fields: fields[6]=f.fields[7]
                self.deliver(message(2,self.serial,fields,(0,)),pw); self.serial+=1; return
            finally:
                if pw is not None: os.close(pw)
        if f.fd_count:
            if not negotiated: raise protocol.ProtocolError('FD without negotiation')
            if f.message_type==1 and not f.flags&1:
                self.deliver(protocol.make_error(f,self.serial)); self.serial+=1
            elif f.message_type in (2,3): self.tunnel.send(b'D',protocol.make_error(f,f.serial))
            return
        self.tunnel.send(b'D',f.data)


def run_connection(role,client,upstream,broker_path):
    tunnel=Tunnel(client if role=='host' else upstream); adapter=None
    def abort():
        for s in (client,upstream):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
    try:
        tunnel.sock.settimeout(10)
        if role=='host':
            host_auth(tunnel,upstream); adapter=Host(tunnel,broker_path,upstream)
            def reader():
                try:
                    while True:
                        f,fds=receive_fds(upstream)
                        if f is None: break
                        adapter.from_host(f,fds)
                except Exception as exc: LOG.warning('host D-Bus reader: %s',exc)
                finally: abort()
        else:
            negotiated=guest_auth(client,tunnel); adapter=Guest(tunnel,broker_path,client)
            def reader():
                try:
                    while True:
                        f=protocol.read_frame(client)
                        if f is None: break
                        adapter.from_app(f,negotiated)
                except Exception as exc: LOG.warning('guest D-Bus reader: %s',exc)
                finally: abort()
        tunnel.sock.settimeout(None)
        thread=threading.Thread(target=reader,daemon=True); thread.start()
        while True:
            kind,payload=tunnel.recv()
            if kind==b'M': adapter.media(payload)
            elif kind==b'X':
                key=record(payload)['session']
                if role=='host': adapter.fail(key)
                else: adapter.close_session(key)
            elif kind==b'D':
                if role=='host': adapter.from_guest(payload)
                else: adapter.deliver(frame(payload).data)
            elif kind==b'S' and role=='guest': adapter.start(payload)
            elif kind==b'N' and role=='host': adapter.ready(payload)
            else: raise protocol.ProtocolError('unexpected tunnel record')
    except Exception as exc: LOG.info('%s connection ended: %s',role,exc)
    finally:
        abort()
        if adapter: adapter.close()
        client.close(); upstream.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--role',choices=('host','guest'),required=True)
    p.add_argument('--listen',required=True); p.add_argument('--upstream',required=True); p.add_argument('--broker',required=True)
    a=p.parse_args(); logging.basicConfig(level=logging.INFO)
    raise_fd_limit()
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(a.listen); os.chmod(a.listen,0o600); listener.listen(32)
    slots=threading.BoundedSemaphore(64)
    def handle(client):
        up=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            up.connect(a.upstream); run_connection(a.role,client,up,a.broker)
        except OSError as exc: LOG.warning('upstream: %s',exc)
        finally: client.close(); up.close(); slots.release()
    try:
        while True:
            # Never let one failed accept (EMFILE and the like) end the loop:
            # that would disconnect every app of the VM.
            try: client,_=listener.accept()
            except OSError as exc:
                LOG.warning('accept failed: %s',exc)
                if exc.errno in (errno.EMFILE,errno.ENFILE,errno.ENOBUFS,errno.ENOMEM): time.sleep(0.1)
                continue
            if not slots.acquire(False): client.close(); continue
            threading.Thread(target=handle,args=(client,),daemon=True).start()
    finally: listener.close(); os.unlink(a.listen)

if __name__=='__main__': main()
