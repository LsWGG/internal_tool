import {parseDocument} from 'yaml'

// Display configuration without resolving environment variables or touching Docker.
export function composeMetadata(content) {
  const empty = {image: [], published_ports: []}
  try {
    const document = parseDocument(content || '')
    if (document.errors.length) return empty
    const config = document.toJS({maxAliasCount: 100})
    const services = config?.services
    if (!services || typeof services !== 'object' || Array.isArray(services)) return empty
    for (const service of Object.values(services)) {
      if (!service || typeof service !== 'object') continue
      if (typeof service.image === 'string') empty.image.push(service.image)
      for (const port of Array.isArray(service.ports) ? service.ports : []) {
        if (typeof port === 'string' || typeof port === 'number') {
          // Preserve ranges, IPv6 addresses, protocols and unresolved ${VARIABLES}.
          empty.published_ports.push(String(port))
        } else if (port && typeof port === 'object' && port.target != null) {
          const host = port.host_ip ? `${port.host_ip}:` : ''
          const published = port.published ?? '自动分配'
          empty.published_ports.push(`${host}${published} → ${port.target}/${port.protocol || 'tcp'}`)
        }
      }
    }
    return Object.fromEntries(Object.entries(empty).map(([key, values]) => [key, [...new Set(values)]]))
  } catch {
    return empty
  }
}
