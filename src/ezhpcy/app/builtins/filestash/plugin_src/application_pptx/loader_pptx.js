// Presentations (pptx) in Filestash, drawn by pptx-glimpse: every slide as an
// SVG, one under another, as the docx viewer shows pages.
//
// Each slide is an image of its SVG, not inline markup, so that nothing in a
// document's SVG runs in the page.
import { createElement, onDestroy } from "../../lib/skeleton/index.js";
import rxjs, { effect } from "../../lib/rx.js";
import ajax from "../../lib/ajax.js";
import { loadJS, loadCSS } from "../../helpers/loader.js";
import { transition } from "../../pages/viewerpage/common.js";
import ctrlDownloader, { init as initDownloader } from "../../pages/viewerpage/application_downloader.js";
import { buttonDownload } from "../../pages/viewerpage/component_menubar.js";
import { createLoader } from "../../components/loader.js";

export default async function(render, { getDownloadUrl, getFilename, $menubar, acl$ }) {
    const $page = createElement(`
        <div class="component_pptx"></div>
    `);
    render($page);
    $menubar.add(buttonDownload(getDownloadUrl()));

    const urls = [];
    onDestroy(() => urls.forEach((url) => URL.revokeObjectURL(url)));
    const removeLoader = createLoader($page);
    effect(ajax({ url: getDownloadUrl(), responseType: "arraybuffer" }).pipe(
        rxjs.mergeMap(async ({ response }) => renderSlides(response, $page, urls)),
        removeLoader,
        rxjs.catchError(() => ctrlDownloader(render, { acl$, getFilename, getDownloadUrl, hasMenubar: false })),
    ));
}

async function renderSlides(response, $page, urls) {
    // Native text in the browser's fonts: a browser has no system fonts to give.
    const report = await window.PptxGlimpse.convertPptxToSvg(new Uint8Array(response), { textOutput: "text" });
    if (report.slides.length === 0) throw new Error("no slides rendered");
    $page.classList.add("hidden");
    for (const { slideNumber, svg } of report.slides) {
        const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
        urls.push(url);
        const $slide = createElement(`<img class="slide" draggable="false">`);
        $slide.setAttribute("alt", `slide ${slideNumber}`);
        $slide.setAttribute("src", url);
        $page.appendChild($slide);
    }
    $page.classList.remove("hidden");
    $page.parentElement.classList.add("scroll-y");
    transition($page);
}

await (async function init() {
    return Promise.all([
        initDownloader(),
        loadCSS(import.meta.url, "./loader_pptx.css"),
        loadJS(import.meta.url, "./lib/vendor/pptx-glimpse.min.js"),
    ]);
})();
