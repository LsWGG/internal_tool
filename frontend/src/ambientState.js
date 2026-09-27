/* Pause bus for the ambient canvas layer.
   The canvas is a fixed, full-viewport surface, so anything that already animates the page
   (a Leaflet drag, a wheel zoom, a table scroll) should stop it rather than compete with it.
   Kept separate from the component so other modules can pause the layer without importing it. */

const reasons = new Set()
const subscribers = new Set()

function emit() {
  const paused = reasons.size > 0
  for (const notify of subscribers) notify(paused)
}

export function setAmbientPaused(reason, paused) {
  const had = reasons.has(reason)
  if (paused === had) return
  if (paused) reasons.add(reason)
  else reasons.delete(reason)
  emit()
}

export function subscribeAmbient(notify) {
  subscribers.add(notify)
  return () => subscribers.delete(notify)
}
