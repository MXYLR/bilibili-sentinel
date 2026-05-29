import redis
r = redis.Redis(host="localhost", port=6379, db=1)
keys = r.keys("*")
print(f"Redis db=1 keys: {len(keys)}")
for k in sorted(keys):
    kstr = k.decode()
    t = r.type(k).decode()
    size = r.llen(k) if t == "list" else r.zcard(k) if t == "zset" else "?"
    print(f"  {kstr}: {size} ({t})")
