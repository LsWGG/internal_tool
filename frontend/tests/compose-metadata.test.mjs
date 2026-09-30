import test from 'node:test'
import assert from 'node:assert/strict'
import {composeMetadata} from '../src/composeMetadata.js'

test('reads images and short/long ports without evaluating variables', () => {
  assert.deepEqual(composeMetadata(`services:
  app:
    image: nginx:alpine
    ports:
      - "8080:80"
      - "[::1]:8443:443/tcp"
      - "\${PORT}:81"
      - target: 53
        published: "5353"
        host_ip: 127.0.0.1
        protocol: udp
      - target: 90
`), {image: ['nginx:alpine'], published_ports: ['8080:80', '[::1]:8443:443/tcp', '${PORT}:81', '127.0.0.1:5353 → 53/udp', '自动分配 → 90/tcp']})
})
test('handles invalid YAML, missing services and build-only projects', () => {
  for (const content of ['services: [', '', 'services: []', 'services:\n  app:\n    build: .']) {
    assert.deepEqual(composeMetadata(content), {image: [], published_ports: []})
  }
})
test('supports anchors and deduplicates metadata', () => {
  assert.deepEqual(composeMetadata('services:\n  a: &app\n    image: redis:7\n    ports: ["6379:6379"]\n  b: *app'), {image: ['redis:7'], published_ports: ['6379:6379']})
})
