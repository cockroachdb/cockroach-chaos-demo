// Survive a region outage: the primary region loses power under load; payments recover and no money is lost.
import { survive } from './_survive.mjs';

export default survive({
  id: 'survive-region',
  title: 'Survive a region outage',
  premise: 'A whole AWS region goes dark while payments are flowing.',
  kind: 'region',
});
