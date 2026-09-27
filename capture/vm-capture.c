#define _GNU_SOURCE
#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/buffer/meta.h>
#include <spa/param/buffers.h>
#include <drm_fourcc.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <gbm.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include "nvgpu_capture.h"

/* Fixed-size little-endian protocol, no host pointers or file descriptors.
 * Tokens travel only on the inherited private channel. */
enum { CONFIG = 1, BUFFER, READY, FRAME, DONE };
struct packet {
    uint32_t magic, op, slot, id, width, height, format, count;
    uint64_t modifier, seq;
    unsigned char token[16];
};
_Static_assert(sizeof(struct packet) == 64, "wire packet");
struct inj_reply {
    uint32_t op; int32_t status; uint32_t id, version;
    unsigned char token[16]; uint32_t max_buffers, max_syncobjs; uint64_t max_bytes;
};
struct inj_import {
    uint32_t op, planes, width, height, fourcc, flags;
    uint64_t modifier; uint32_t offsets[4], strides[4];
};
struct slot {
    struct pw_buffer *pw;
    struct nvgpu_capture_open imported;
    uint32_t id;
    uint64_t seq, sent_ns;
    struct spa_region crop;
    uint32_t transform;
    bool available, pending, imported_ok;
};
struct app {
    bool guest, ready, configured, stopping, streaming;
    int channel, status, inject, capture, render, result;
    uint32_t count, assigned;
    uint64_t sequence;
    struct pw_main_loop *loop;
    struct pw_context *context;
    struct pw_core *core;
    struct pw_stream *stream;
    struct spa_hook listener, core_listener;
    struct spa_video_info_raw format;
    struct slot slots[32];
    unsigned char input[sizeof(struct packet)]; size_t input_used;
};
static void fail(struct app *a, const char *why) {
    if (!a->stopping) fprintf(stderr, "vm-capture: %s (%s)\n", why, strerror(errno));
    a->stopping = true; a->result = 1;
    pw_main_loop_quit(a->loop);
}
static void core_error(void *data, uint32_t id, int seq, int res, const char *message) {
    struct app *a=data; (void)id; (void)seq;
    errno=res<0?-res:EIO;
    fail(a,message?message:"PipeWire core error");
}
static const struct pw_core_events core_events={PW_VERSION_CORE_EVENTS,.error=core_error};
static uint64_t now_ns(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static int send_packet(struct app *a, struct packet p) {
    p.magic = htole32(0x31504356); p.op = htole32(p.op);
    p.slot = htole32(p.slot); p.id = htole32(p.id);
    p.width = htole32(p.width); p.height = htole32(p.height);
    p.format = htole32(p.format); p.count = htole32(p.count);
    p.modifier = htole64(p.modifier); p.seq = htole64(p.seq);
    /* A tiny control packet, but use a bounded deadline if transport stalls. */
    size_t n = 0;
    uint64_t deadline = now_ns() + 250000000ULL;
    while (n < sizeof p) {
        if (now_ns() >= deadline) { errno=ETIMEDOUT; fail(a,"capture control write deadline exceeded"); return -1; }
        ssize_t r = send(a->channel, (char *)&p + n, sizeof p - n, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (r > 0) { n += r; continue; }
        if (r < 0 && errno == EINTR) continue;
        if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd fd = {.fd=a->channel,.events=POLLOUT};
            uint64_t now=now_ns();
            int remaining=now<deadline?(int)((deadline-now+999999)/1000000):0;
            if (remaining && poll(&fd, 1, remaining) > 0) continue;
        }
        fail(a, "capture control channel write failed"); return -1;
    }
    return 0;
}
static int inject_request(struct app *a, const void *p, size_t size, int *fds, size_t nfds, struct inj_reply *reply) {
    struct iovec v = {.iov_base=(void *)p,.iov_len=size};
    char ancillary[CMSG_SPACE(4 * sizeof(int))] = {0};
    struct msghdr m = {.msg_iov=&v,.msg_iovlen=1};
    if (nfds) {
        m.msg_control=ancillary; m.msg_controllen=CMSG_SPACE(nfds*sizeof(int));
        struct cmsghdr *c=CMSG_FIRSTHDR(&m); c->cmsg_level=SOL_SOCKET; c->cmsg_type=SCM_RIGHTS;
        c->cmsg_len=CMSG_LEN(nfds*sizeof(int)); memcpy(CMSG_DATA(c),fds,nfds*sizeof(int));
    }
    if (sendmsg(a->inject,&m,MSG_NOSIGNAL)!=(ssize_t)size) return -1;
    if (recv(a->inject,reply,sizeof *reply,MSG_TRUNC)!=(ssize_t)sizeof *reply) return -1;
    if (le32toh(reply->op) != le32toh(*(const uint32_t *)p)) { errno=EPROTO; return -1; }
    if (reply->status) { errno=-(int32_t)le32toh(reply->status); return -1; }
    return 0;
}
static uint32_t fourcc(uint32_t f) {
    switch(f) {
    case SPA_VIDEO_FORMAT_BGRx: return DRM_FORMAT_XRGB8888;
    case SPA_VIDEO_FORMAT_BGRA: return DRM_FORMAT_ARGB8888;
    default: return 0;
    }
}
static void param_changed(void *data, uint32_t id, const struct spa_pod *param) {
    struct app *a=data;
    if (id!=SPA_PARAM_Format || !param) return;
    struct spa_video_info_raw f={0};
    if (spa_format_video_raw_parse(param,&f)<0 || !fourcc(f.format) ||
        !f.size.width || !f.size.height || f.size.width>8192 || f.size.height>8192 ||
        !(f.flags & SPA_VIDEO_FLAG_MODIFIER)) { errno=EINVAL; fail(a,"unsupported capture format; DMA-BUF required"); return; }
    if (a->configured && (f.format!=a->format.format || f.size.width!=a->format.size.width ||
        f.size.height!=a->format.size.height || f.modifier!=a->format.modifier)) {
        errno=ENOTSUP; fail(a,"capture format changed; restart screen sharing"); return;
    }
    a->format=f; a->configured=true;
    if (!a->guest) send_packet(a,(struct packet){.op=CONFIG,.width=f.size.width,.height=f.size.height,.format=f.format,.modifier=f.modifier});
    unsigned char storage[1024]; struct spa_pod_builder b=SPA_POD_BUILDER_INIT(storage,sizeof storage);
    const struct spa_pod *params[4];
    params[0]=spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamBuffers,SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers,SPA_POD_CHOICE_RANGE_Int(a->guest?a->count:8,a->guest?a->count:2,a->guest?a->count:32),
        SPA_PARAM_BUFFERS_blocks,SPA_POD_Int(1),
        SPA_PARAM_BUFFERS_dataType,SPA_POD_CHOICE_FLAGS_Int(1<<SPA_DATA_DmaBuf));
    params[1]=spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamMeta,SPA_PARAM_Meta,
        SPA_PARAM_META_type,SPA_POD_Id(SPA_META_Header),SPA_PARAM_META_size,SPA_POD_Int(sizeof(struct spa_meta_header)));
    params[2]=spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamMeta,SPA_PARAM_Meta,
        SPA_PARAM_META_type,SPA_POD_Id(SPA_META_VideoCrop),SPA_PARAM_META_size,SPA_POD_Int(sizeof(struct spa_meta_region)));
    params[3]=spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamMeta,SPA_PARAM_Meta,
        SPA_PARAM_META_type,SPA_POD_Id(SPA_META_VideoTransform),SPA_PARAM_META_size,SPA_POD_Int(sizeof(struct spa_meta_videotransform)));
    pw_stream_update_params(a->stream,params,4);
}
static void add_buffer(void *data, struct pw_buffer *b) {
    struct app *a=data; struct spa_buffer *buf=b->buffer;
    if (a->stopping) return;
    if (a->guest) {
        if (a->assigned>=a->count || buf->n_datas!=1) { errno=EPROTO; fail(a,"invalid guest buffer count"); return; }
        struct slot *s=&a->slots[a->assigned++]; s->pw=b;
        struct spa_data *d=buf->datas;
        if (!(d->type & (1<<SPA_DATA_DmaBuf))) { errno=ENOTSUP; fail(a,"guest consumer needs DMA-BUF support"); return; }
        d->type=SPA_DATA_DmaBuf; d->flags=SPA_DATA_FLAG_READABLE;
        d->fd=s->imported.dmabuf_fd; d->mapoffset=0;
        d->maxsize=s->imported.size;
        d->chunk->offset=s->imported.offsets[0]; d->chunk->stride=s->imported.strides[0];
        d->chunk->size=s->imported.strides[0]*a->format.size.height;
        return;
    }
    if (a->ready || a->count>=32 || buf->n_datas!=1 || buf->datas[0].type!=SPA_DATA_DmaBuf) {
        errno=ENOTSUP; fail(a,"unsupported host buffer pool or reallocation"); return;
    }
    uint32_t n=a->count; struct slot *s=&a->slots[n]; s->pw=b;
    struct spa_data *d=buf->datas;
    /* The producer supplies DMA-BUF layout in add_buffer (verified against
     * xdph's pwStreamAddBuffer). Reject an incomplete layout rather than
     * inventing a stride that could map unrelated pixels. */
    if (!d->chunk || d->chunk->stride <= 0 || d->fd < 0 ||
        (uint64_t)d->mapoffset + d->chunk->offset > UINT32_MAX) {
        errno=EPROTO; fail(a,"host DMA-BUF layout missing at allocation"); return;
    }
    struct inj_import req={.op=htole32(2),.planes=htole32(1),.width=htole32(a->format.size.width),
        .height=htole32(a->format.size.height),.fourcc=htole32(fourcc(a->format.format)),.modifier=htole64(a->format.modifier)};
    req.offsets[0]=htole32(d->mapoffset+d->chunk->offset); req.strides[0]=htole32(d->chunk->stride);
    int fd=d->fd; struct inj_reply r;
    if (inject_request(a,&req,sizeof req,&fd,1,&r)<0) { fail(a,"injecting host DMA-BUF failed"); return; }
    s->id=le32toh(r.id);
    s->imported.offsets[0]=d->mapoffset+d->chunk->offset;
    s->imported.strides[0]=d->chunk->stride;
    a->count++;
    struct packet p={.op=BUFFER,.slot=n,.id=s->id}; memcpy(p.token,r.token,16); send_packet(a,p);
}
static void remove_buffer(void *data, struct pw_buffer *b) {
    struct app *a=data;
    if (!a->stopping) { errno=ENOTSUP; fail(a,"capture buffer pool removed; restart sharing"); }
    (void)b;
}
static void guest_process(struct app *a) {
    struct pw_buffer *b;
    while ((b=pw_stream_dequeue_buffer(a->stream))) {
        for (uint32_t i=0;i<a->count;i++) if (a->slots[i].pw==b) {
            struct slot *s=&a->slots[i];
            if (s->sent_ns) { send_packet(a,(struct packet){.op=DONE,.slot=i,.seq=s->seq}); s->sent_ns=0; }
            s->available=true; break;
        }
    }
    for (uint32_t i=0;i<a->count;i++) {
        struct slot *s=&a->slots[i];
        if (!s->available || !s->pending) continue;
        struct spa_meta_header *h=spa_buffer_find_meta_data(s->pw->buffer,SPA_META_Header,sizeof *h);
        if (h) { h->seq=s->seq; h->pts=now_ns(); h->flags=0; h->dts_offset=0; }
        struct spa_meta_region *crop=spa_buffer_find_meta_data(s->pw->buffer,SPA_META_VideoCrop,sizeof *crop);
        struct spa_meta_videotransform *transform=spa_buffer_find_meta_data(s->pw->buffer,SPA_META_VideoTransform,sizeof *transform);
        if(crop)crop->region=s->crop;
        if(transform)transform->transform=s->transform;
        s->pending=false; s->available=false; s->sent_ns=now_ns();
        pw_stream_queue_buffer(a->stream,s->pw);
    }
}
static void process(void *data) {
    struct app *a=data; if (a->stopping) return;
    if (a->guest) { guest_process(a); return; }
    if (!a->ready) return;
    struct pw_buffer *b;
    while ((b=pw_stream_dequeue_buffer(a->stream))) {
        uint32_t i;
        for(i=0;i<a->count;i++) if(a->slots[i].pw==b) break;
        if(i==a->count || a->slots[i].sent_ns) { errno=EPROTO; fail(a,"unknown or busy host buffer"); return; }
        /* Explicit acquire fences are not negotiated. Never publish unsynchronized buffers. */
        if(spa_buffer_find_meta(b->buffer,SPA_META_SyncTimeline)) { errno=ENOTSUP; fail(a,"explicit-sync capture is not supported yet"); return; }
        struct spa_data *d=b->buffer->datas;
        if(d->chunk->stride!=(int32_t)a->slots[i].imported.strides[0] ||
           (uint64_t)d->mapoffset+d->chunk->offset!=a->slots[i].imported.offsets[0]) {
            errno=ENOTSUP;fail(a,"capture buffer layout changed");return;
        }
        struct spa_meta_header *header=spa_buffer_find_meta_data(b->buffer,SPA_META_Header,sizeof *header);
        if((d->chunk->flags & SPA_CHUNK_FLAG_CORRUPTED) || !d->chunk->size ||
           (header && (header->flags & SPA_META_HEADER_FLAG_CORRUPTED))) {
            pw_stream_queue_buffer(a->stream,b); continue;
        }
        struct spa_meta_region *crop=spa_buffer_find_meta_data(b->buffer,SPA_META_VideoCrop,sizeof *crop);
        struct spa_meta_videotransform *transform=spa_buffer_find_meta_data(b->buffer,SPA_META_VideoTransform,sizeof *transform);
        struct spa_region region=SPA_REGION(0,0,a->format.size.width,a->format.size.height);
        if(crop && crop->region.size.width && crop->region.size.height)region=crop->region;
        if(region.position.x<0 || region.position.y<0 ||
           (uint64_t)region.position.x+region.size.width>a->format.size.width ||
           (uint64_t)region.position.y+region.size.height>a->format.size.height ||
           (transform && transform->transform>7)) { errno=EPROTO;fail(a,"invalid capture crop/transform");return; }
        struct slot *s=&a->slots[i]; s->seq=++a->sequence; s->sent_ns=now_ns();
        send_packet(a,(struct packet){.op=FRAME,.slot=i,.seq=s->seq,
            .id=region.position.x,.count=region.position.y,.width=region.size.width,.height=region.size.height,
            .format=transform?transform->transform:0});
    }
}
static void state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error) {
    struct app *a=data; (void)old;
    a->streaming=state==PW_STREAM_STATE_STREAMING;
    if(state==PW_STREAM_STATE_ERROR) { errno=EIO; fail(a,error?error:"PipeWire stream failed"); }
    if(a->guest && state==PW_STREAM_STATE_PAUSED && !a->ready) {
        a->ready=true;
        if(dprintf(a->status,"{\"node\":%u}\n",pw_stream_get_node_id(a->stream))<0) fail(a,"cannot report guest source node");
    }
    if(!a->guest && state==PW_STREAM_STATE_STREAMING && !a->ready) {
        if(!a->count) { errno=EPROTO; fail(a,"empty capture pool"); return; }
        a->ready=true; send_packet(a,(struct packet){.op=READY,.count=a->count});
    }
}
static const struct pw_stream_events events={PW_VERSION_STREAM_EVENTS,.state_changed=state_changed,
    .param_changed=param_changed,.add_buffer=add_buffer,.remove_buffer=remove_buffer,.process=process};
static int publish(struct app *a) {
    a->stream=pw_stream_new(a->core,"VM screen capture",pw_properties_new(
        PW_KEY_MEDIA_CLASS,"Video/Source",PW_KEY_MEDIA_TYPE,"Video",PW_KEY_MEDIA_CATEGORY,"Capture",
        PW_KEY_NODE_NAME,"vm-screen-capture",PW_KEY_NODE_DESCRIPTION,"Shared host screen",NULL));
    if(!a->stream) return -1;
    pw_stream_add_listener(a->stream,&a->listener,&events,a);
    unsigned char storage[512]; struct spa_pod_builder b=SPA_POD_BUILDER_INIT(storage,sizeof storage);
    const struct spa_pod *p=spa_format_video_raw_build(&b,SPA_PARAM_EnumFormat,&a->format);
    return pw_stream_connect(a->stream,PW_DIRECTION_OUTPUT,PW_ID_ANY,
        PW_STREAM_FLAG_DRIVER|PW_STREAM_FLAG_ALLOC_BUFFERS,&p,1);
}
static void receive_packet(struct app *a, struct packet p) {
    p.op=le32toh(p.op);p.slot=le32toh(p.slot);p.id=le32toh(p.id);p.seq=le64toh(p.seq);
    p.width=le32toh(p.width);p.height=le32toh(p.height);p.format=le32toh(p.format);p.count=le32toh(p.count);
    if(le32toh(p.magic)!=0x31504356 || p.slot>=32) goto invalid;
    if(!a->guest) {
        if(p.op!=DONE || p.slot>=a->count || !a->slots[p.slot].sent_ns || a->slots[p.slot].seq!=p.seq) goto invalid;
        a->slots[p.slot].sent_ns=0; pw_stream_queue_buffer(a->stream,a->slots[p.slot].pw); return;
    }
    if(p.op==CONFIG && !a->configured) {
        if(!p.width || !p.height || p.width>8192 || p.height>8192 || !fourcc(p.format)) goto invalid;
        a->format=(struct spa_video_info_raw){.format=p.format,.size=SPA_RECTANGLE(p.width,p.height),
            .flags=SPA_VIDEO_FLAG_MODIFIER,.modifier=le64toh(p.modifier),.framerate=SPA_FRACTION(0,1)};
        a->configured=true; return;
    }
    if(p.op==BUFFER && a->configured && !a->stream && p.slot==a->count) {
        struct slot *s=&a->slots[p.slot];
        s->imported=(struct nvgpu_capture_open){.render_fd=a->render,.id=p.id}; memcpy(s->imported.token,p.token,16);
        if(ioctl(a->capture,NVGPU_CAPTURE_IOC_OPEN,&s->imported)<0) { fail(a,"opening injected buffer failed"); return; }
        s->imported_ok=true;
        if(s->imported.nplanes!=1 || s->imported.width!=a->format.size.width || s->imported.height!=a->format.size.height ||
            s->imported.fourcc!=fourcc(a->format.format) || s->imported.modifier!=a->format.modifier ||
            s->imported.buf_flags || s->imported.size>UINT32_MAX) goto invalid;
        a->count++; return;
    }
    if(p.op==READY && !a->stream && a->count && p.count==a->count) {
        if(publish(a)<0) fail(a,"publishing guest source failed");
        return;
    }
    if(p.op==FRAME && a->stream && p.slot<a->count) {
        struct slot *s=&a->slots[p.slot];
        if(s->pending || s->sent_ns || !p.seq || p.seq<=a->sequence) goto invalid;
        if(!p.width || !p.height || (uint64_t)p.id+p.width>a->format.size.width ||
           (uint64_t)p.count+p.height>a->format.size.height || p.format>7)goto invalid;
        s->crop=SPA_REGION(p.id,p.count,p.width,p.height);s->transform=p.format;
        a->sequence=p.seq;s->seq=p.seq;s->pending=true;
        if(a->streaming)pw_stream_trigger_process(a->stream);
        return;
    }
invalid:
    errno=EPROTO; fail(a,"invalid capture control message");
}
static void channel_ready(void *data, int fd, uint32_t mask) {
    struct app *a=data;(void)mask;
    for(;;) {
        ssize_t n=recv(fd,a->input+a->input_used,sizeof a->input-a->input_used,MSG_DONTWAIT);
        if(n<0 && errno==EINTR) continue;
        if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) return;
        if(n<=0) { errno=ECONNRESET;fail(a,"capture channel closed");return; }
        a->input_used+=n;
        if(a->input_used==sizeof a->input) { struct packet p;memcpy(&p,a->input,sizeof p);a->input_used=0;receive_packet(a,p); }
        if(a->stopping) return;
    }
}
static void timer(void *data,uint64_t count) {
    struct app *a=data;(void)count;
    if(a->guest) { if(a->stream && a->streaming)pw_stream_trigger_process(a->stream); return; }
    for(uint32_t i=0;i<a->count;i++) if(a->slots[i].sent_ns && now_ns()-a->slots[i].sent_ns>5000000000ULL) {
        errno=ETIMEDOUT;fail(a,"guest did not return capture buffer within five seconds");return;
    }
}
static void quit(void *data,int sig) {struct app *a=data;(void)sig;a->stopping=true;pw_main_loop_quit(a->loop);}
/* Query modifiers on the actual render device; advertise only formats the
 * same device can import. The injection backend independently verifies GPU
 * ownership and layout before granting the guest an import token. */
static const struct spa_pod *host_format(struct spa_pod_builder *b, uint32_t format,
                                       EGLDisplay display) {
    EGLuint64KHR modifiers[128]; EGLBoolean external[128]; EGLint count=0;
    PFNEGLQUERYDMABUFMODIFIERSEXTPROC query=(void *)eglGetProcAddress("eglQueryDmaBufModifiersEXT");
    if(display!=EGL_NO_DISPLAY && query)
        query(display,fourcc(format),128,modifiers,external,&count);
    if(count<=0) { modifiers[0]=DRM_FORMAT_MOD_LINEAR; count=1; }
    if(count>128)count=128;
    struct spa_pod_frame object,choice;
    spa_pod_builder_push_object(b,&object,SPA_TYPE_OBJECT_Format,SPA_PARAM_EnumFormat);
    spa_pod_builder_add(b,SPA_FORMAT_mediaType,SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype,SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format,SPA_POD_Id(format),0);
    spa_pod_builder_prop(b,SPA_FORMAT_VIDEO_modifier,SPA_POD_PROP_FLAG_MANDATORY|SPA_POD_PROP_FLAG_DONT_FIXATE);
    spa_pod_builder_push_choice(b,&choice,SPA_CHOICE_Enum,0);
    spa_pod_builder_long(b,modifiers[0]);
    for(int i=0;i<count;i++)spa_pod_builder_long(b,modifiers[i]);
    spa_pod_builder_pop(b,&choice);
    spa_pod_builder_add(b,
        SPA_FORMAT_VIDEO_size,SPA_POD_CHOICE_RANGE_Rectangle(&SPA_RECTANGLE(1920,1080),&SPA_RECTANGLE(1,1),&SPA_RECTANGLE(8192,8192)),
        SPA_FORMAT_VIDEO_framerate,SPA_POD_CHOICE_RANGE_Fraction(&SPA_FRACTION(30,1),&SPA_FRACTION(0,1),&SPA_FRACTION(60,1)),0);
    return spa_pod_builder_pop(b,&object);
}
int main(int argc,char **argv) {
    struct app a={.channel=-1,.status=-1,.inject=-1,.capture=-1,.render=-1};
    const char *inject=NULL,*render="/dev/dri/renderD128",*capture="/dev/nvgpu-capture";int pwfd=-1;uint32_t node=PW_ID_ANY;
    if(argc<2 || (strcmp(argv[1],"host") && strcmp(argv[1],"guest"))) return 2;
    a.guest=!strcmp(argv[1],"guest");
    for(int i=2;i+1<argc;i+=2) {
        if(!strcmp(argv[i],"--control-fd"))a.channel=atoi(argv[i+1]);
        else if(!strcmp(argv[i],"--pipewire-fd"))pwfd=atoi(argv[i+1]);
        else if(!strcmp(argv[i],"--status-fd"))a.status=atoi(argv[i+1]);
        else if(!strcmp(argv[i],"--node-id"))node=strtoul(argv[i+1],NULL,10);
        else if(!strcmp(argv[i],"--inject-socket"))inject=argv[i+1];
        else if(!strcmp(argv[i],"--render-node"))render=argv[i+1];
        else if(!strcmp(argv[i],"--capture-device"))capture=argv[i+1];
        else return 2;
    }
    if(a.channel<0 || (a.guest?a.status<0:pwfd<0 || !inject || node==PW_ID_ANY)) return 2;
    signal(SIGPIPE,SIG_IGN);pw_init(NULL,NULL);a.loop=pw_main_loop_new(NULL);
    if(!a.loop)return 1;
    if(a.guest) {
        a.capture=open(capture,O_RDWR|O_CLOEXEC);a.render=open(render,O_RDWR|O_CLOEXEC);
        if(a.capture<0 || a.render<0){perror("vm-capture: capture device");return 1;}
    } else {
        struct sockaddr_un addr={.sun_family=AF_UNIX};
        if(strlen(inject)>=sizeof addr.sun_path)return 2;
        strcpy(addr.sun_path,inject);
        a.inject=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0);
        struct timeval timeout={.tv_sec=2};setsockopt(a.inject,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof timeout);setsockopt(a.inject,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof timeout);
        if(a.inject<0 || connect(a.inject,(void *)&addr,sizeof addr)<0){perror("vm-capture: inject connect");return 1;}
        uint32_t hello[4]={htole32(1),htole32(1),0,0};struct inj_reply reply;
        if(inject_request(&a,hello,sizeof hello,NULL,0,&reply)<0){perror("vm-capture: inject hello");return 1;}
    }
    a.context=pw_context_new(pw_main_loop_get_loop(a.loop),NULL,0);
    a.core=a.guest?pw_context_connect(a.context,NULL,0):pw_context_connect_fd(a.context,pwfd,NULL,0);
    if(!a.core){perror("vm-capture: PipeWire connect");return 1;}
    pw_core_add_listener(a.core,&a.core_listener,&core_events,&a);
    pw_loop_add_io(pw_main_loop_get_loop(a.loop),a.channel,SPA_IO_IN|SPA_IO_HUP|SPA_IO_ERR,false,channel_ready,&a);
    pw_loop_add_signal(pw_main_loop_get_loop(a.loop),SIGTERM,quit,&a);
    pw_loop_add_signal(pw_main_loop_get_loop(a.loop),SIGINT,quit,&a);
    struct spa_source *tick=pw_loop_add_timer(pw_main_loop_get_loop(a.loop),timer,&a);
    struct timespec interval={.tv_nsec=10000000};pw_loop_update_timer(pw_main_loop_get_loop(a.loop),tick,&interval,&interval,false);
    if(!a.guest) {
        a.stream=pw_stream_new(a.core,"VM host capture",pw_properties_new(PW_KEY_MEDIA_TYPE,"Video",PW_KEY_MEDIA_CATEGORY,"Capture",NULL));
        pw_stream_add_listener(a.stream,&a.listener,&events,&a);
        int drm=open(render,O_RDWR|O_CLOEXEC);
        struct gbm_device *gbm=drm>=0?gbm_create_device(drm):NULL;
        EGLDisplay display=gbm?eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR,gbm,NULL):EGL_NO_DISPLAY;
        if(display!=EGL_NO_DISPLAY && !eglInitialize(display,NULL,NULL))display=EGL_NO_DISPLAY;
        if(display==EGL_NO_DISPLAY)fprintf(stderr,"vm-capture: EGL modifier query unavailable; trying linear buffers\n");
        unsigned char storage[4096];struct spa_pod_builder b=SPA_POD_BUILDER_INIT(storage,sizeof storage);
        const struct spa_pod *params[2]={host_format(&b,SPA_VIDEO_FORMAT_BGRx,display),host_format(&b,SPA_VIDEO_FORMAT_BGRA,display)};
        int res=pw_stream_connect(a.stream,PW_DIRECTION_INPUT,node,PW_STREAM_FLAG_AUTOCONNECT,params,2);
        if(display!=EGL_NO_DISPLAY)eglTerminate(display);
        if(gbm)gbm_device_destroy(gbm);
        if(drm>=0)close(drm);
        if(res<0){perror("vm-capture: host stream");return 1;}
    }
    pw_main_loop_run(a.loop);a.stopping=true;
    if(a.stream)pw_stream_destroy(a.stream);
    pw_context_destroy(a.context);pw_main_loop_destroy(a.loop);
    for(unsigned i=0;i<32;i++)if(a.slots[i].imported_ok)close(a.slots[i].imported.dmabuf_fd);
    if(a.inject>=0)close(a.inject);
    if(a.capture>=0)close(a.capture);
    if(a.render>=0)close(a.render);
    close(a.channel);pw_deinit();return a.result;
}
