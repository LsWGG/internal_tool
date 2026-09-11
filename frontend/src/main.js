import { createApp } from 'vue'
import 'leaflet/dist/leaflet.css'
import '@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.css'
import './style.css'
import './ui-refinement.css'
import './selectMenu.css'
import { installSelectMenus } from './selectMenu'
import App from './App.vue'
createApp(App).mount('#app')
installSelectMenus()
