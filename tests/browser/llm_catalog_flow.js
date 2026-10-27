// Run with Playwright browser_run_code_unsafe against llm_catalog_fixture_server.py on 19872.
async (page) => {
  const base = "http://127.0.0.1:19872";
  const check = (ok, message) => { if (!ok) throw Error(message); };
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto(`${base}/app/login`);
  await page.locator('[name="api_key"]').fill("fixture-app");
  await page.getByRole("button", {name: "登录", exact: true}).click();
  await page.goto(`${base}/app/settings#llm/catalog`);
  await page.getByRole("button", {name: "添加 Provider", exact: true}).waitFor();
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
      await item.getByLabel("模型标识", {exact: true}).fill(model);
    }
  }
  await page.getByLabel("投资 Agent 模型", {exact: true}).selectOption({label: "主 Provider / 推理模型"});
  await page.getByLabel("研究解释与规则草拟模型", {exact: true}).selectOption({label: "备用 Provider / 备用模型"});
  await page.locator("#settings-save").click();
  await page.locator("#settings-save-result").getByText("已保存 1 项：Provider 与模型目录。", {exact: true}).waitFor();
  await page.reload();
  await page.getByRole("button", {name: "添加 Provider", exact: true}).waitFor();
  check(await page.locator(".llm-provider").count() === 2, "Providers were not persisted");
  check(await page.locator(".llm-model").count() === 3, "Models were not persisted");
  const primary = page.locator(".llm-provider").first();
  check(await primary.getByLabel("API Key", {exact: true}).inputValue() === "", "Credential echoed into DOM");
  check(await primary.getByRole("button", {name: "删除 Provider", exact: true}).isDisabled(), "Referenced provider can be deleted");
  check(await primary.locator(".llm-model").nth(1).getByRole("button", {name: "删除模型", exact: true}).isDisabled(), "Referenced model can be deleted");

  // Credential omission survives ordinary edits and topic navigation.
  await primary.getByLabel("Provider 名称", {exact: true}).fill("已编辑 Provider");
  await page.goto(`${base}/app/settings#agent/model`);
  await page.getByRole("button", {name: "选择 Provider / Model", exact: true}).click();
  await page.getByLabel("Provider 名称", {exact: true}).first().waitFor();
  check(await page.getByLabel("Provider 名称", {exact: true}).first().inputValue() === "已编辑 Provider", "Unsaved draft lost while navigating topics");
  await page.locator("#settings-save").click();
  await page.locator("#settings-save-result").getByText("已保存 1 项：Provider 与模型目录。", {exact: true}).waitFor();

  const response = await page.request.get(`${base}/settings`, {headers: {"X-API-Key": "fixture-app"}});
  const saved = await response.json();
  check(saved.llm_catalog.providers[0].configured, "Unchanged key was lost");
  check(!JSON.stringify(saved).includes("sk-ui-primary-1234"), "Credential leaked in response");

  // Unused model removal persists; active selections remain intact.
  await primary.locator(".llm-model").first().getByRole("button", {name: "删除模型", exact: true}).click();
  await page.locator("#settings-save").click();
  await page.locator("#settings-save-result").getByText("已保存 1 项：Provider 与模型目录。", {exact: true}).waitFor();
  check(await page.locator(".llm-model").count() === 2, "Unused model deletion failed");

  await page.goto(`${base}/app/settings#agent/model`);
  await page.locator("#settings-form").getByText("目录中的 Agent 模型：已编辑 Provider / 推理模型。", {exact: false}).waitFor();
  await page.getByRole("button", {name: "选择 Provider / Model", exact: true}).click();
  await page.getByRole("button", {name: "添加 Provider", exact: true}).waitFor();
  await page.setViewportSize({width: 390, height: 844});
  check(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), "Mobile page overflows horizontally");
  await page.getByLabel("投资 Agent 模型", {exact: true}).selectOption({label: "备用 Provider / 备用模型"});
  await page.locator("#settings-save").click();
  await page.locator("#settings-save-result").getByText("已保存 1 项：Provider 与模型目录。", {exact: true}).waitFor();
  await page.reload();
  await page.getByRole("button", {name: "添加 Provider", exact: true}).waitFor();
  const select = page.getByLabel("投资 Agent 模型", {exact: true});
  check(await select.locator("option:checked").innerText() === "备用 Provider / 备用模型", "Mobile model switch was not persisted");
  await page.screenshot({path: "/tmp/zcharlie-catalog-final-mobile.png", fullPage: true});
  await page.setViewportSize({width: 1440, height: 1000});
  await page.screenshot({path: "/tmp/zcharlie-catalog-final-desktop.png", fullPage: true});
  check(errors.length === 0, errors.join("\n"));
  return {passed: true, providers: 2, models: 2, desktop: true, mobile: true, keysMasked: true};
}
