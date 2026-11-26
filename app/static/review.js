/* 现场复核批次页的渐进增强：全选裂缝、把行内裂缝号带到顶部提交表单。
   所有核心操作都是普通 form POST，没有 JS 也能完成。 */
(function () {
  "use strict";

  /* 全选 / 全不选（仅在当前未全选时全选，否则清空） */
  var pickAll = document.getElementById("pick-all-cracks");
  if (pickAll) {
    pickAll.addEventListener("click", function () {
      var boxes = document.querySelectorAll(
        '#batch-create-form input[name="crack_ids"]'
      );
      var allChecked = Array.prototype.every.call(boxes, function (b) {
        return b.checked;
      });
      boxes.forEach(function (b) { b.checked = !allChecked; });
    });
  }

  /* 待复核行的“填入↑”：把裂缝号写入顶部提交表单并聚焦 X 坐标 */
  var crackInput = document.getElementById("rv-crack");
  document.querySelectorAll(".js-fill-crack").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (!crackInput) { return; }
      crackInput.value = btn.getAttribute("data-crack") || "";
      var xInput = document.querySelector(
        '#review-submit-form input[name="x"]'
      );
      if (xInput) { xInput.focus(); }
      document.getElementById("review-submit-form")
        .scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });

  /* 表单被拒后服务端直接 400 重渲染（URL 无 #review）：把错误横幅滚入视野 */
  if (!window.location.hash) {
    var errBanner = document.querySelector("#review .review-banner.banner-error");
    if (errBanner) {
      errBanner.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
})();
