<script setup>
import Header from "@/components/Header.vue"
import { store, popupsRef, currentSettings } from "@/assets/store"
import Popups from "@/components/Popups.vue"
import { addPopup, getDataOrPopupError } from "@/assets/utils";
import { ref, watch } from "vue";


async function lazyCheckUpdate() {
  let updateCheckInfo = await getDataOrPopupError("/utils/lazy_check_update")
  if (updateCheckInfo.has_new_version) {
    addPopup("blue", "检测到新版！", "请到Github或pypi下载最新版")
  }
}

setTimeout(lazyCheckUpdate, 0)

setTimeout(async () => {
  // evil hack to ensure color transition enabled after theme color being set
  setTimeout(() => store.theme_background_transition = true, 100)
  let settings = await getDataOrPopupError("/settings")
  for (let key of Object.keys(settings)) {
    currentSettings[key] = settings[key]
  }
}, 0)

watch(() => currentSettings.fontSize, (newValue) => {
  let targetVal = newValue
  if (newValue < 8 || newValue > 100) {
    targetVal = 16
  }
  document.querySelector("html").style.fontSize = `${targetVal}px`
})

</script>

<template>
  <div id="root" :data-theme="store.theme">
    <!-- modified button from https://www.svgrepo.com/collection/dazzle-line-icons/ -->
    <Header />
    <main>
      <router-view></router-view>
    </main>
  </div>
  <Popups ref="popupsRef" />
</template>

<style>
#root {
  display: flex;
  align-items: center;
  flex-direction: column;
  height: 95vh;
}


main {
  height: 50vh;
  width: 90%;
  flex-grow: 1;
  margin-top: 30px;
  display: flex;
  flex-direction: column;
}
</style>
