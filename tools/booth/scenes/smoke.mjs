// Not a booth video: exercises the camera, pointer and captions without touching the cluster.
export default {
  id: 'smoke',
  title: 'Recorder smoke test',
  premise: 'Camera, pointer and captions on the live stage.',
  async run(d) {
    await d.say('This is the live stage: nine nodes in three AWS regions.');
    await d.shot('region:us-east-1');
    await d.say('Each tile is a node. The line is its heartbeat.');
    await d.shot('kpi:money+kpi:recovery');
    await d.say('A close-up of two numbers.');
    await d.shot('wide');
    await d.hover('kpi:nodes');
    await d.say('Hovering a number explains it.');
    await d.click('node:us-east-1a-n1');
    await d.say('Clicking a node opens its menu.');
    await d.key('Escape');
    await d.shot('chart');
    await d.say('The chart, close up.');
    await d.shot('wide');
  },
  outro: () => ['Smoke test done'],
};
