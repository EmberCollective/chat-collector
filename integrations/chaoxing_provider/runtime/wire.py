"""Bounded protobuf reader; no platform or file access."""
def fields(raw):
    if len(raw)>2_000_000:raise ValueError('Message too large')
    p=0;out=[]
    def varint():
        nonlocal p
        value=0
        for shift in range(0,70,7):
            if p>=len(raw):raise ValueError('Truncated varint')
            n=raw[p];p+=1;value|=(n&127)<<shift
            if n<128:return value
        raise ValueError('Invalid varint')
    while p<len(raw):
        tag=varint();key,wire=tag>>3,tag&7
        if not key or len(out)>10000:raise ValueError('Invalid wire')
        if wire==0:value=varint()
        elif wire in (1,5):
            size=8 if wire==1 else 4;value=raw[p:p+size];p+=size
        elif wire==2:
            size=varint();value=raw[p:p+size];p+=size
            if len(value)!=size:raise ValueError('Truncated field')
        else:raise ValueError('Unsupported wire')
        if p>len(raw):raise ValueError('Truncated field')
        out.append((key,value))
    return out

