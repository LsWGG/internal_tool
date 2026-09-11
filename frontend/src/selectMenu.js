// Keep native select bindings and change events; render a shared, in-page menu.
export function installSelectMenus() {
  let active = null
  let sequence = 0
  function close() {
    if (!active) return
    active.observer.disconnect()
    active.select.setAttribute('aria-expanded', 'false')
    active.select.removeAttribute('aria-controls')
    active.select.removeAttribute('aria-activedescendant')
    active.menu.remove()
    active = null
  }
  function position() {
    if (!active) return
    const {select, menu} = active
    if (!select.isConnected || select.disabled) return close()
    const rect = select.getBoundingClientRect()
    const width = Math.min(Math.max(rect.width, 220), innerWidth - 24)
    const below = innerHeight - rect.bottom - 12
    const above = rect.top - 12
    const height = Math.min(300, Math.max(80, below >= above ? below : above))
    Object.assign(menu.style, {width: width+'px', maxHeight: height+'px', left: Math.max(12, Math.min(rect.left, innerWidth-width-12))+'px', top: (below >= above ? rect.bottom+6 : Math.max(12, rect.top-menu.offsetHeight-6))+'px'})
  }
  function highlight(index) {
    if (!active) return
    active.index = index
    for (const item of active.menu.querySelectorAll('[role=option]')) item.classList.toggle('focused', Number(item.dataset.index) === index)
    const item = active.menu.querySelector(`[data-index="${index}"]`)
    if (item) {
      active.select.setAttribute('aria-activedescendant', item.id)
      item.scrollIntoView({block:'nearest'})
    }
  }
  function choose(index) {
    if (!active) return
    const select = active.select
    const option = select.options[index]
    if (!option || option.disabled || option.parentElement.disabled) return
    const changed = select.selectedIndex !== index
    select.selectedIndex = index
    close()
    select.focus({preventScroll:true})
    if (changed) {
      select.dispatchEvent(new Event('input', {bubbles:true}))
      select.dispatchEvent(new Event('change', {bubbles:true}))
    }
  }
  function render() {
    if (!active) return
    const {select, menu} = active
    menu.replaceChildren()
    let group = null
    Array.from(select.options).forEach((option, index) => {
      if (option.hidden) return
      const parent = option.parentElement
      if (parent.tagName === 'OPTGROUP' && group !== parent) {
        group = parent
        const label = document.createElement('div')
        label.className = 'ui-select-group'; label.textContent = parent.label
        menu.append(label)
      }
      const item = document.createElement('div')
      item.id = `${menu.id}-${index}`; item.dataset.index = index
      item.setAttribute('role','option')
      item.setAttribute('aria-selected', String(option.selected))
      item.setAttribute('aria-disabled', String(option.disabled || !!parent.disabled))
      item.textContent = option.textContent
      item.title = option.textContent
      item.addEventListener('pointerdown', event => event.preventDefault())
      item.addEventListener('click', event => {event.stopPropagation(); choose(index)})
      menu.append(item)
    })
    position()
    highlight(select.selectedIndex)
  }
  function open(select) {
    close()
    const menu = document.createElement('div')
    menu.className='ui-select-menu'; menu.id=`ui-select-${++sequence}`
    menu.setAttribute('role','listbox')
    menu.setAttribute('aria-label', select.getAttribute('aria-label') || select.labels?.[0]?.textContent.trim().slice(0,80) || '选择选项')
    document.body.append(menu)
    active={select,menu,index:select.selectedIndex,observer:new MutationObserver(render)}
    active.observer.observe(select,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled','hidden','label','value','selected'],characterData:true})
    select.focus({preventScroll:true})
    select.setAttribute('aria-expanded','true'); select.setAttribute('aria-controls',menu.id)
    render()
  }
  function eligible(target) {return target instanceof HTMLSelectElement && !target.disabled && !target.multiple && target.size <= 1}
  document.addEventListener('pointerdown', event => {
    if (eligible(event.target)) {
      event.preventDefault()
      if (active?.select === event.target) close(); else open(event.target)
    } else if (active && !active.menu.contains(event.target)) close()
  },true)
  document.addEventListener('keydown', event => {
    if (!eligible(event.target)) return
    if (event.key === 'Tab') return close()
    if (event.key === 'Escape') {event.preventDefault(); return close()}
    if (['ArrowDown','ArrowUp','Home','End','Enter',' '].includes(event.key)) {
      event.preventDefault()
      if (!active) return open(event.target)
      if (event.key === 'Enter' || event.key === ' ') return choose(active.index)
      const indices=Array.from(active.select.options).map((o,i)=>!o.disabled&&!o.hidden&&!o.parentElement.disabled?i:-1).filter(i=>i>=0)
      let pos=indices.indexOf(active.index)
      pos=event.key==='Home'?0:event.key==='End'?indices.length-1:Math.max(0,Math.min(indices.length-1,pos+(event.key==='ArrowUp'?-1:1)))
      highlight(indices[pos])
    } else if(active && event.key.length===1 && !event.ctrlKey && !event.metaKey) {
      event.preventDefault()
      const option=Array.from(active.select.options).findIndex(o=>!o.disabled&&!o.parentElement.disabled&&o.text.toLowerCase().startsWith(event.key.toLowerCase()))
      if(option>=0)highlight(option)
    }
  },true)
  document.addEventListener('focusin', event => {if(active && event.target!==active.select && !active.menu.contains(event.target))close()})
  window.addEventListener('resize',close)
  window.addEventListener('hashchange',close)
  document.addEventListener('scroll',event=>{if(active && !active.menu.contains(event.target))position()},true)
}
