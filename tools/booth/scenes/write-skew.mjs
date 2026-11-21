// Isolation levels: the same race between two withdrawals breaks a rule under READ COMMITTED and keeps it under SERIALIZABLE.
import { clusterFacts, startScenario } from './_common.mjs';

export default {
  id: 'write-skew',
  title: 'Isolation levels: write skew',
  premise: 'Two withdrawals at the same instant. Does the rule of the account survive?',

  async prepare(d) {
    await clusterFacts(d);
    await startScenario(d, 'write-skew');
  },

  async run(d) {
    await d.shot('scenario');
    await d.say('A joint account: $50 in checking, $50 in savings. The rule: together they never go below $0.');
    await d.say('Two people each withdraw $100 at the same instant, one from each account.');

    const loose = (await d.step()).result;                           // Race under READ COMMITTED
    d.check(loose['From checking'] === 'committed' && loose['From savings'] === 'committed' && String(loose['Rule (≥ $0)']).startsWith('BROKEN'),
      'under READ COMMITTED both withdrawals committed and the rule broke');
    d.fact('looseTotal', loose['Checking + savings'].replace('$-', '-$'));
    await d.say('Under READ COMMITTED, a weaker isolation level, each one checks, sees $100 available, and commits.');
    await d.say('Together the accounts are at {looseTotal}. The rule is broken, and no error was raised.');
    await d.say('This is write skew: each transaction was valid on its own, and wrong together.');

    const strict = (await d.step()).result;                          // Race under SERIALIZABLE
    const outcomes = [strict['From checking'], strict['From savings']];
    d.check(outcomes.filter((o) => String(o).startsWith('committed')).length === 1 && outcomes.includes('declined') && String(strict['Rule (≥ $0)']).startsWith('kept'),
      'under SERIALIZABLE one withdrawal committed, the other was declined, and the rule held');
    d.fact('strictTotal', strict['Checking + savings']);
    await d.say("Now the same race under SERIALIZABLE, CockroachDB's default.");
    await d.say('One withdrawal commits. The other is retried, sees the new balance, and is declined.');
    await d.say('The accounts total {strictTotal}. The rule holds, with no extra code in the application.');
  },

  outro: (f) => [`READ COMMITTED: both withdrawals commit, balance ${f.looseTotal}`, `SERIALIZABLE: one commits, one is declined, balance ${f.strictTotal}`,
    'SERIALIZABLE is the CockroachDB default'],
};
