// Time travel: every order is deleted by mistake, then read back as of a moment earlier and restored with plain SQL.
import { clusterFacts, startScenario } from './_common.mjs';

export default {
  id: 'time-travel',
  title: 'Time travel',
  premise: 'Someone deletes every order by mistake. Get them back with plain SQL.',

  async prepare(d) {
    await clusterFacts(d);
    await startScenario(d, 'time-travel');
  },

  async run(d) {
    const f = d.facts;
    await d.shot('scenario');
    await d.say('An orders table. Then someone runs a DELETE and forgets the WHERE clause.');
    const gone = (await d.step()).result;                            // Accidental DELETE
    d.check(gone['Rows left'] === 0, 'the DELETE removed every row');
    await d.say('Every order is gone. The table is empty.');
    await d.say('No backup to hunt for, no restore to wait on. Ask the database for the past instead.');

    const past = (await d.step()).result;                            // Look back in time
    d.check(past['Rows one moment ago'] > 0, 'the rows can still be read as of a moment earlier');
    d.fact('orders', past['Rows one moment ago']);
    await d.say('SELECT ... AS OF SYSTEM TIME reads the table as it was a moment before: {orders} orders.');
    await d.say('CockroachDB keeps recent versions of every row, so the past stays readable for a set window.');

    const restored = (await d.step()).result;                        // Restore
    d.check(restored['Rows restored'] === f.orders, 'every order was restored');
    await d.say('Read the rows as they were, write them back: all {orders} orders restored.');
    await d.shot('wide');
    await d.say('The cluster stayed online the whole time. Nothing else was touched.');
  },

  outro: (f) => [`${f.orders} orders deleted by mistake`, 'Read back as of a moment earlier, with plain SQL', `All ${f.orders} restored`],
};
