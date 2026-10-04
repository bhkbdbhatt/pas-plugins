import { mount } from 'svelte';
import App from './App.svelte';

// The tenant is read from local storage rather than hard-coded, because the
// same build is used against several tenants during an integration.
const stored = localStorage.getItem('pas.tenantId');

// Svelte 5 mounts a component rather than constructing it, so this is `mount`
// and not `new App(...)`.
mount(App, {
  target: document.getElementById('app')!,
  props: { tenantId: stored ?? 'demo-carrier', port: 8006 }
});
