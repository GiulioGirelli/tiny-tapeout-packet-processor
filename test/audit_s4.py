"""Independent, public-pin S4 audit; no imports from the project model/helpers.
Run: make -B COCOTB_TEST_MODULES=audit_s4
Includes the owner-approved 1B / 2B / 3A behavior (18 September 2026).
Runs in the default RTL and gate-level regression as well as standalone.
"""
import random
import cocotb
from cocotb.triggers import Timer

class Pins:
    def __init__(self, dut):
        self.d = dut
        self.cycles = 0
    async def cycle(self, byte=0, cfg=0, ps=0, reset=0, ena=1, rst_n=1, want=None):
        d=self.d
        d.clk.value=0
        d.ui_in.value=byte
        d.uio_in.value=cfg | (ps << 1) | (reset << 3) | 0xE0
        d.ena.value=ena
        d.rst_n.value=rst_n
        await Timer(5, unit='ns')
        d.clk.value=1
        await Timer(5, unit='ns')
        got=(int(d.uo_out.value), int(d.uio_out.value) >> 5)
        assert int(d.uio_oe.value)==0xE0
        assert int(d.uio_out.value)&31==0
        if want is not None:
            assert got==want, f'cycle {self.cycles}: got={got}, want={want}, input={(byte,cfg,ps,reset,ena,rst_n)}'
        self.cycles+=1
        return got
    async def reset(self):
        await self.cycle(rst_n=0, want=(0,0))
        await self.cycle(want=(0,0))
    async def write(self, a, v, ok=True):
        await self.cycle(a,1,2,want=(0,0))
        await self.cycle(v,1,3,want=(v,3) if ok else (0,5))
    async def read(self,a,v):
        await self.cycle(a,1,1,want=(v,6))

@cocotb.test()
async def programmed_classification_1500_cases(dut):
    p=Pins(dut)
    rng=random.Random(0x53415544)
    cases=[]
    for winner in range(3):
        for mask in range(1,8):
            for action in range(16):
                cases.append((1<<winner, winner, mask, action))
    for hits in range(8):
        for action in range(16):
            cases.append((hits, 2, 7, action))
    while len(cases)<1500:
        cases.append((rng.randrange(8),rng.randrange(3),rng.randrange(1,8),rng.randrange(16)))
    winners=set(); masks_seen=set(); actions_seen=set(); hitsets=set()
    for number,(hitset, forced_rule, forced_mask, forced_action) in enumerate(cases):
        await p.reset()
        types=rng.sample(range(3,256),3)
        words=[rng.randrange(65536) for _ in range(3)]
        regs=types+[0]*21
        flags=[]; rule_words=[]
        for r in range(3):
            mask=forced_mask if r==forced_rule else rng.randrange(1,8)
            action=forced_action if r==forced_rule else rng.randrange(16)
            vals=list(words)
            enabled=1
            if not (hitset>>r)&1:
                if rng.randrange(2):
                    enabled=0
                else:
                    chosen=rng.choice([t for t in range(3) if (mask>>t)&1])
                    vals[chosen]^=1<<rng.randrange(16)
            flag=(action<<4)|(mask<<1)|enabled
            flags.append(flag); rule_words.append(vals)
            regs[3+7*r:10+7*r]=[flag]+[b for word in vals for b in (word>>8,word&255)]
        for a,v in enumerate(regs):
            await p.write(a | (rng.randrange(8)<<5),v)
        for a,v in enumerate(regs):
            await p.read(a | (rng.randrange(8)<<5),v)
        matches=[r for r in range(3) if flags[r]&1 and all(not (flags[r]&(2<<t)) or words[t]==rule_words[r][t] for t in range(3))]
        assert sum(1<<r for r in matches)==hitset
        winner=max(matches)+1 if matches else 0
        result=0
        if winner:
            f=flags[winner-1]; mask=(f>>1)&7
            result=(f&240)|((mask.bit_count()-1)<<2)|winner
            masks_seen.add(mask); actions_seen.add(f>>4)
        winners.add(winner); hitsets.add(hitset)
        data=[[v>>8,v&255] for v in words]
        counts=[0]*3; nbytes=0; full=False
        # Split and interleave all slots, preserving byte order within each slot.
        while not full:
            t=rng.choice([i for i in range(3) if counts[i]<2])
            await p.cycle(types[t],ps=2,want=(0,1)); nbytes+=1
            await p.cycle(data[t][counts[t]],ps=1,want=(result,2) if sum(counts)==5 else (0,1)); nbytes+=1
            counts[t]+=1; full=all(x==2 for x in counts)
            if not full and rng.randrange(3)==0:
                unknown=next(v for v in range(256) if v not in types)
                await p.cycle(unknown,ps=2,want=(0,1)); nbytes+=1
                for _ in range(rng.randrange(3)):
                    await p.cycle(rng.randrange(256),ps=1,want=(0,1)); nbytes+=1
        # Full slots never change, even with repeated metadata and overflow.
        for _ in range(rng.randrange(4)):
            await p.cycle(rng.choice(types),ps=2,want=(result,2)); nbytes+=1
            await p.cycle(rng.randrange(256),ps=1,want=(result,2)); nbytes+=1
        await p.cycle(ps=3,want=(result,2))
        expected=regs+[nbytes>>8,nbytes&255,0,1]+[int(winner==i) for i in [1,2,3,0]]
        for a,v in enumerate(expected):
            await p.read(a,v)
        if number%300==0:
            dut._log.info('independent classification cases: %d/1500',number)
    assert winners=={0,1,2,3} and masks_seen==set(range(1,8))
    assert actions_seen==set(range(16)) and hitsets==set(range(8))
    dut._log.info('Independent classification PASS: 1500 cases; all winners, masks, actions, hit combinations; %d pin cycles',p.cycles)

@cocotb.test()
async def all_flag_values_and_address_aliases(dut):
    p=Pins(dut)
    await p.reset()
    for a in [3,10,17]:
        previous=0
        for v in range(256):
            ok=(v&15)!=1
            await p.write(a|((v&7)<<5),v,ok)
            if ok: previous=v
            await p.read(a|((v&7)<<5),previous)
    for a in range(24,32):
        for high in range(8):
            await p.write(a|(high<<5),0xCC,False)
            await p.read(a|(high<<5),0)
    for a in range(3):
        await p.write(a,a)
        for other in range(3):
            if a!=other:
                await p.write(a,other,False)
                await p.read(a,a)

@cocotb.test()
async def natural_counter_wraps(dut):
    p=Pins(dut)
    await p.reset()
    # Every metadata starts a packet; early EOP never counts a rule/no-rule hit.
    for i in range(65537):
        await p.cycle(0,ps=2,want=(0,1))
        await p.cycle(ps=3,want=(0,4))
    for a,v in zip(range(24,32),[0,1,0,1,0,0,0,0]):
        await p.read(a,v)
    await p.cycle(reset=3,want=(0,0))
    # Overflow and irrelevant bytes are still counted as accepted bytes.
    await p.cycle(0xFF,ps=2,want=(0,1))
    for i in range(65536):
        await p.cycle(i&255,ps=1,want=(0,1))
    await p.cycle(ps=3,want=(0,4))
    for a,v in zip(range(24,32),[0,1,0,1,0,0,0,0]):
        await p.read(a,v)
    dut._log.info('Natural 16-bit packet and byte wraps PASS: %d pin cycles',p.cycles)

@cocotb.test()
async def soft_reset_transients_expire_persistent_states_survive(dut):
    """1B: one-cycle indications expire; ongoing packet/error state survives."""
    p = Pins(dut)
    for reset in [2, 3]:
        for state in [6, 3, 5, 4, 2]:
            await p.reset()
            if state == 6:
                await p.cycle(1, 1, 1, want=(1, 6))
            elif state == 3:
                await p.cycle(4, 1, 2, want=(0, 0))
                await p.cycle(0xAA, 1, 3, want=(0xAA, 3))
            elif state == 5:
                await p.cycle(0xFF, 1, 3, want=(0, 5))
            elif state == 4:
                await p.cycle(0, ps=2, want=(0, 1))
                await p.cycle(ps=3, want=(0, 4))
            else:
                # A nonzero result must expire too, after EOP.
                await p.write(3, 0xA3)  # rule 1, type 1 == 0, action A
                for t in range(3):
                    await p.cycle(t, ps=2)
                    await p.cycle(0, ps=1)
                    await p.cycle(0, ps=1)
                await p.cycle(ps=3, want=(0xA1, 2))
            for _ in range(2):
                await p.cycle(0xFF, cfg=1, ps=3, reset=reset, want=(0, 0))
            await p.cycle(want=(0, 0))

        for state in [1, 2, 7]:
            await p.reset()
            result = 0
            if state == 1:
                await p.cycle(0, ps=2, want=(0, 1))
            elif state == 2:
                await p.write(3, 0xA3)
                for t in range(3):
                    await p.cycle(t, ps=2)
                    await p.cycle(0, ps=1)
                    await p.cycle(0, ps=1)
                result = 0xA1
            else:
                await p.cycle(ps=1, want=(0, 7))
            for _ in range(2):
                # Would-be configuration traffic is ignored on reset cycles.
                await p.cycle(0xFF, cfg=1, ps=3, reset=reset,
                              want=(result, state))
            if state == 2:
                await p.cycle(ps=3, want=(result, 2))
                await p.read(0x1C, 1)  # original captured winner counted once
            elif state == 1:
                await p.cycle(ps=3, want=(0, 4))
            else:
                await p.cycle(want=(0, 7))
                await p.cycle(reset=1, want=(0, 0))


@cocotb.test()
async def type_reset_preserves_chunk_selection(dut):
    """2B: matched AND unmatched selection survives type reset until metadata."""
    p = Pins(dut)
    await p.reset()
    await p.write(0, 0x80)
    await p.cycle(0x80, ps=2, want=(0, 1))
    await p.cycle(0xAA, ps=1, want=(0, 1))
    await p.cycle(reset=2, want=(0, 1))
    # Old metadata continues using the already-selected slot.
    await p.cycle(0xBB, ps=1, want=(0, 1))
    for t in [1, 2]:
        await p.cycle(t, ps=2, want=(0, 1))
        await p.cycle(0, ps=1, want=(0, 1))
        await p.cycle(0, ps=1, want=(0, 2) if t == 2 else (0, 1))
    await p.cycle(ps=3, want=(0, 2))
    await p.read(0x1F, 1)
    await p.read(0, 0)  # type reset did take effect

    # New metadata re-evaluates the type mapping: 0x80 is now irrelevant.
    await p.cycle(0x80, ps=2, want=(0, 1))
    await p.cycle(0xCC, ps=1, want=(0, 1))
    await p.cycle(0xDD, ps=1, want=(0, 1))
    for t in [1, 2]:
        await p.cycle(t, ps=2, want=(0, 1))
        await p.cycle(0, ps=1, want=(0, 1))
        await p.cycle(0, ps=1, want=(0, 1))
    await p.cycle(ps=3, want=(0, 4))
    await p.read(0x1F, 1)

    # Previously unmatched metadata stays unmatched across reset too.
    await p.reset()
    await p.write(0, 0x80)
    await p.cycle(0, ps=2, want=(0, 1))
    await p.cycle(reset=2, want=(0, 1))
    await p.cycle(0xAA, ps=1, want=(0, 1))
    await p.cycle(0xBB, ps=1, want=(0, 1))
    for t in [1, 2]:
        await p.cycle(t, ps=2, want=(0, 1))
        await p.cycle(0, ps=1, want=(0, 1))
        await p.cycle(0, ps=1, want=(0, 1))
    await p.cycle(0, ps=2, want=(0, 1))  # new metadata uses reset type value
    await p.cycle(0xAA, ps=1, want=(0, 1))
    await p.cycle(0xBB, ps=1, want=(0, 2))
    await p.cycle(ps=3, want=(0, 2))
    await p.read(0x1F, 1)


@cocotb.test()
async def synchronous_enable_and_asynchronous_hard_reset(dut):
    """3A: ena is sampled at an edge; hard reset needs no edge, even disabled."""
    p = Pins(dut)
    await p.reset()
    await p.cycle(1, 1, 1, want=(1, 6))
    dut.clk.value = 0
    dut.ena.value = 0
    await Timer(2, unit='ns')
    assert int(dut.uo_out.value) == 1
    assert int(dut.uio_out.value) == (6 << 5)
    await p.cycle(ena=0, want=(0, 0))  # next rising edge forces idle
    await p.cycle(0xFF, cfg=1, ps=3, reset=2, ena=0, want=(0, 0))
    await p.read(1, 1)
    dut.clk.value = 0
    dut.ena.value = 0
    dut.rst_n.value = 0
    await Timer(2, unit='ns')
    assert int(dut.uo_out.value) == 0 and int(dut.uio_out.value) == 0
    assert int(dut.uio_oe.value) == 0xE0
