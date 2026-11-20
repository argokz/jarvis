import { createApp } from "vue";
import App from "./App.vue";
import { applyTokens } from "./lib/tokens";
import "./style.css";

applyTokens(document.documentElement); // до первого кадра: страница сразу в своей палитре
createApp(App).mount("#app");
