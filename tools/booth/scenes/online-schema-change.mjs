// Online schema change: add a column and build an index on a table while payments keep writing to it.
import { clusterFacts, money, startScenario } from './_common.mjs';

/** Payments a second while a change ran: they slow a little, and the caption says so. */
const rate = (count, text) => Math.round(Number(count) / Math.max(1, parseFloat(String(text).replace(/^done in /, ''))) / 10) * 10;
const took = (text) => { const n = Math.round(parseFloat(String(text).replace(/^done in /, ''))); return `${n} second${n === 1 ? '' : 's'}`; };

export default {
  id: 'online-schema-change',
  title: 'Online schema change',
  premise: 'Change a table while payments are writing to it.',

  async prepare(d) {
    await clusterFacts(d);
    await startScenario(d, 'online-schema-change');
  },

  async run(d) {
    await d.say('A live payments system: {nodes} nodes across {regions} {cloud} regions.');
    const start = await d.next();                                    // Start payments
    await d.say('Every payment updates two rows of the accounts table.', { wait: false });
    await start.done();
    const flowing = await d.until((s) => s.workload.running && s.workload.tps > 50 && s.workload.invariant_ok && s, { what: 'payments to flow' });
    d.fact('tps', Math.round(flowing.workload.tps / 50) * 50);
    d.fact('total', money(flowing.workload.expected));
    await d.shot('kpi:tps+kpi:p99+kpi:money+kpi:recovery');
    await d.say('About {tps} payments a second are writing to that table right now.');

    await d.shot('wide');
    await d.say('Now change the table while it is in use. First, add a column.');
    const column = await d.next();                                   // ADD COLUMN
    await d.say('ALTER TABLE accounts ADD COLUMN memo, with a default filled in for every row.', { wait: false });
    const added = (await column.done({ timeout: 1500 })).result;
    d.check(added['Failed transfers meanwhile'] === 0, 'no payment failed while the column was added');
    d.fact('columnTook', took(added['ADD COLUMN']));
    d.fact('columnPayments', Number(added['Transfers meanwhile']).toLocaleString('en-US'));
    d.fact('columnRate', rate(added['Transfers meanwhile'], added['ADD COLUMN']));
    await d.shot('scenario');
    await d.say('Done in {columnTook}. {columnPayments} payments committed meanwhile (about {columnRate} a second). None failed.');

    await d.shot('wide');
    await d.say('Next, build an index on the balance column.');
    const index = await d.next();                                    // CREATE INDEX
    await d.say('CREATE INDEX reads every row in the background while the writes continue.', { wait: false });
    const built = (await index.done({ timeout: 1500 })).result;
    d.check(built['Failed transfers meanwhile'] === 0, 'no payment failed while the index was built');
    d.fact('indexTook', took(built['CREATE INDEX']));
    d.fact('indexPayments', Number(built['Transfers meanwhile']).toLocaleString('en-US'));
    d.fact('indexRate', rate(built['Transfers meanwhile'], built['CREATE INDEX']));
    await d.shot('scenario');
    await d.say('Done in {indexTook}. {indexPayments} payments committed meanwhile (about {indexRate} a second). None failed.');

    const end = await d.state();
    d.check(end.workload.money_lost === 0 && end.workload.invariant_ok, 'the accounts still add up');
    await d.shot('kpi:tps+kpi:p99+kpi:money+kpi:recovery');
    await d.say('The table was never locked. No maintenance window. Money lost: $0.');
    await d.shot('wide');
  },

  outro: (f) => ['A column added and an index built on a live table', `${f.columnPayments} + ${f.indexPayments} payments committed during the changes`, `None failed. $0 lost of ${f.total}`],
};
