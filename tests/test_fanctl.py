import sys, importlib.util, os, tempfile, time
spec = importlib.util.spec_from_file_location("f", sys.argv[1]); f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
D, C = f.DISK_CURVE, f.CPU_CURVE
d = lambda c, t, p=0: f.duty_of(c, f.level(c, t, p))
# thresholds
assert [d(D,t) for t in (20,39,40,44,45,50,53,54,70,200)] == [0,0,5,5,20,40,40,99,99,99]
assert [d(C,t) for t in (40,79,82,83,86,87,90,93,105)] == [0,0,0,5,5,20,60,99,99]
# monotone over whole range from cold start, all duties allowed and '1'-free
for c in (D,C):
    prev = -1
    for t in range(0,150):
        v = d(c,t); assert v >= prev, (t,v); prev = v
        assert v in f.ALLOWED and "1" not in "%02d" % v
# hysteresis: cpu on at 83, off only below 73
assert f.level(C,74,1,10)==1 and f.level(C,73,1,10)==1 and f.level(C,72,1,10)==0
assert f.level(C,84,2,10)==2 and f.level(C,83,2,10)==1  # 87-level drops below 84
assert f.level(D,38,1,3)==1 and f.level(D,36,1,3)==0
# decide(): standby / spinning / unevaluable
class St: disk_lvl=0; cpu_lvl=0
f.data_disks = lambda: ["sda","sdb"]
f.cpu_temp = lambda: 79
f.in_standby = lambda d: True
assert f.decide(St())[0] == 0
f.cpu_temp = lambda: 84
assert f.decide(St())[0] == 5
f.cpu_temp = lambda: 79
f.in_standby = lambda d: d=="sdb"
f.disk_temp = lambda d: 30
assert f.decide(St())[0] == 0
f.disk_temp = lambda d: 47
assert f.decide(St())[0] == 20
def bad(*a): raise f.Unevaluable("x")
f.disk_temp = bad
try: f.decide(St()); raise SystemExit("no raise")
except f.Unevaluable: pass
try: f.plausible(0,"x"); raise SystemExit("no raise")
except f.Unevaluable: pass
p=tempfile.mktemp(); open(p,"w").write("30"); os.utime(p,(time.time()-500,)*2)
try: f.read_fresh(p,120); raise SystemExit("no raise")
except f.Unevaluable: pass
try: f.write_duty(10); raise SystemExit("no raise")
except ValueError: pass
assert f.FAILSAFE in f.ALLOWED and f.EXIT_DUTY in f.ALLOWED and f.KICK_DUTY in f.ALLOWED
# min-on
m=f.apply_min_on
assert m(5,0.0,100)[:2]==(5,100)
assert m(0,100,100+599)[0]==5
assert m(0,100,100+600)[:2]==(0,0.0)
assert m(0,0.0,5)[:2]==(0,0.0)
print("ALL OK")
