# Layer 2: random chains (property-based).
#
# gen.random_chain(seed) draws 1..6 transitions; the generator knows the
# expected stack (gen.py header).  Chains run in batches, one tarantool
# under gdb per batch; JIT on and off; the driver alternates between the
# main fiber and a fiber.  A failed seed is reproduced with
#   LJBT_SEED0=<seed> LJBT_SEEDS=1 tests/run.sh random

import random
import unittest

import common as C
import gen

SEEDS = C.env_int('LJBT_SEEDS', 200)
SEED0 = C.env_int('LJBT_SEED0', 1)
BATCH = 50


class RandomMixin(object):
    jit = 'off'

    def test_random_chains(self):
        problems = []
        seeds = list(range(SEED0, SEED0 + SEEDS))
        for b in range(0, len(seeds), BATCH):
            batch = seeds[b:b + BATCH]
            driver = 'main' if (b // BATCH) % 2 == 0 else 'fiber'
            wd = C.WorkDir('random')
            prog = gen.Program(self.jit == 'on', driver)
            chains = []
            for s in batch:
                ch = gen.random_chain(s, random.Random(s))
                prog.add(ch)
                chains.append(ch)
            stops, oracle, failures, out = C.run_chains(wd, prog)
            for f in failures:
                problems.append('seed {}: the chain failed: {}'.format(
                    f['seq'], f['err']))
            if '<timeout>' in out:
                problems.append('batch {}: gdb timed out'.format(batch))
            for ch in chains:
                p = C.check_chain(ch, stops.get(ch.seq, []), oracle,
                                  gen.tokens, gen.oracle_tokens)
                if p:
                    problems.append('seed {} jit {} driver {} {} -> {}:\n'
                                    '  {}'.format(ch.seq, self.jit, driver,
                                                  ch.transitions, ch.target,
                                                  '\n  '.join(p)))
            if not problems:
                wd.cleanup()
        self.assertEqual(problems, [], '\n'.join(problems))


for _jit in C.jit_modes():
    _name = 'TestRandom_jit_{}'.format(_jit)
    globals()[_name] = type(_name, (RandomMixin, unittest.TestCase),
                            {'jit': _jit})

if __name__ == '__main__':
    unittest.main()
