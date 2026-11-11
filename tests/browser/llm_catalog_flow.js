// Run with Playwright against llm_catalog_fixture_server.py.
async (page) => {
  const base = "http://127.0.0.1:19872";
  const check = (ok, message) => { if (!ok) throw Error(message); };
  const saveAndWait = async (label) => {
    await page.locator("#settings-save").click();
    await page.locator("#settings-save-result").getByText(label, {exact: false}).waitFor();
    await page.waitForFunction(() => document.getElementById("settings-dirty-count")?.textContent === "没有未保存的修改");
  };
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("dialog", (dialog) => dialog.accept());
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto(`${base}/app/login`);
  await page.locator('[name="api_key"]').fill("fixture-app");
  await page.getByRole("button", {name: "登录", exact: true}).click();
  await page.goto(`${base}/app/settings#llm/catalog`);
  await page.getByRole("button", {name: "添加 Provider", exact: true}).waitFor();
  check(await page.getByText("用途分配").count() === 0, "Model page still assigns consumers");
  for (const [name, endpoint, key, models] of [
    ["主 Provider", "https://primary.invalid/v1", "sk-ui-primary-1234", [["快速模型", "fast-model"], ["推理模型", "reason-model"]]],
    ["备用 Provider", "https://secondary.invalid/v1", "sk-ui-secondary-5678", [["备用模型", "secondary-model"]]],
  ]) {
    await page.getByRole("button", {name: "添加 Provider", exact: true}).click();
    const card = page.locator(".llm-provider").last();
    await card.getByLabel("Provider 名称", {exact: true}).fill(name);
    await card.getByLabel("接口地址", {exact: true}).fill(endpoint);
    await card.getByLabel("API Key", {exact: true}).fill(key);
    for (const [display, model] of models) {
      await card.getByRole("button", {name: "添加模型", exact: true}).click();
      const item = card.locator(".llm-model").last();
      await item.getByLabel("模型显示名称", {exact: true}).fill(display);
      await item.getByLabel("Model ID", {exact: true}).fill(model);
      if (display === "推理模型") await item.getByLabel("推理", {exact: true}).check();
      if (display !== "备用模型") await item.getByLabel("工具调用", {exact: true}).check();
    }
  }
  await saveAndWait("Provider 与模型目录");
  await page.reload();
  await page.getByRole("button", {name: "添加 Provider", exact: true}).waitFor();
  check(await page.locator(".llm-provider").count() === 2, "Providers were not persisted");
  check(await page.locator(".llm-model").count() === 3, "Models were not persisted");
  const primary = page.locator(".llm-provider").first();
  check(await primary.getByLabel("API Key", {exact: true}).inputValue() === "", "Credential echoed into DOM");
  check(await primary.getByRole("button", {name: "删除 Provider", exact: true}).isEnabled(), "Provider deletion is blocked by a consumer");

  await primary.getByLabel("Provider 名称", {exact: true}).fill("已编辑 Provider");
  await page.evaluate(() => { location.hash = "#agent"; });
  await page.getByRole("button", {name: /模型/}).first().waitFor();
  await page.evaluate(() => { location.hash = "#llm/catalog"; });
  await page.getByLabel("Provider 名称", {exact: true}).first().waitFor();
  check(await page.getByLabel("Provider 名称", {exact: true}).first().inputValue() === "已编辑 Provider", "Unsaved draft lost while navigating topics");
  await saveAndWait("Provider 与模型目录");

  const response = await page.request.get(`${base}/settings`, {headers: {"X-API-Key": "fixture-app"}});
  const saved = await response.json();
  check(saved.llm_catalog.providers[0].configured, "Unchanged key was lost");
  check(!JSON.stringify(saved).includes("sk-ui-primary-1234"), "Credential leaked in response");
  check(!saved.llm_catalog.bindings, "Catalog still stores bindings");

  await page.evaluate(() => { location.hash = "#agent/models"; });
  await page.getByLabel("主模型", {exact: true}).waitFor();
  const main = page.getByLabel("主模型", {exact: true});
  const optionLabels = await main.locator("option").allTextContents();
  check(optionLabels.some((text) => text.includes("推理模型")), "Capable model missing from slot");
  check(!optionLabels.some((text) => text.includes("备用模型")), "Model without tools was offered for the main slot");
  await main.selectOption({label: "已编辑 Provider / 推理模型"});
  await page.getByLabel("思考模型", {exact: true}).selectOption({label: "已编辑 Provider / 推理模型"});
  await saveAndWait("模型槽位");

  await page.evaluate(() => { location.hash = "#llm/catalog"; });
  await page.getByRole("button", {name: "添加 Provider", exact: true}).waitFor();
  await primary.locator(".llm-model").first().getByRole("button", {name: "删除模型", exact: true}).click();
  await saveAndWait("Provider 与模型目录");
  await page.evaluate(() => { location.hash = "#agent/models"; });
  await page.getByLabel("主模型", {exact: true}).waitFor();
  check((await page.getByLabel("主模型").locator("option:checked").innerText()).includes("推理模型"), "Selected model was cleared");

  await page.setViewportSize({width: 390, height: 844});
  check(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), "Mobile page overflows horizontally");
  await page.screenshot({path: "/tmp/zcharlie-catalog-final-mobile.png", fullPage: true});
  check(errors.length === 0, errors.join("\n"));
  return "ok";
}
