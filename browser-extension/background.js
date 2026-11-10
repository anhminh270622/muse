/* Session values are returned only to this extension's isolated content script. */
function allowedSender(sender) {
  try {
    const u = new URL(sender.url);
    return sender.id === chrome.runtime.id && sender.frameId === 0 &&
      u.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(u.hostname) &&
      ['8765', '8766'].includes(u.port) && u.pathname === '/';
  } catch { return false; }
}

async function collectSession(sender) {
  if (!allowedSender(sender)) throw new Error('Trang kết nối không hợp lệ.');
  const tabs = await chrome.tabs.query({url: ['https://muse.ai/*', 'https://www.muse.ai/*']});
  const tab = tabs.filter(t => !t.discarded).sort((a,b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0];
  if (!tab) throw new Error('Mở tab Muse đã đăng nhập trong cùng hồ sơ Chrome, rồi quay lại bấm Kết nối.');
  const stores = await chrome.cookies.getAllCookieStores();
  const store = stores.find(s => s.tabIds.includes(tab.id));
  if (!store) throw new Error('Không xác định được hồ sơ của tab Muse. Hãy tải lại tab Muse.');
  const raw = await chrome.cookies.getAll({domain: 'muse.ai', storeId: store.id});
  const cookies = raw.filter(c => !c.partitionKey).map(c => ({
    name: c.name, value: c.value, domain: c.domain, path: c.path,
    expires: c.session ? -1 : c.expirationDate,
    httpOnly: c.httpOnly, secure: c.secure,
    sameSite: ({strict:'Strict', lax:'Lax', no_restriction:'None'})[c.sameSite] || 'Lax'
  }));
  const results = await chrome.scripting.executeScript({
    target: {tabId: tab.id},
    func: () => ({
      origin: location.origin,
      localStorage: Object.keys(localStorage).map(name => ({name, value: localStorage.getItem(name)})),
      sessionStorage: Object.keys(sessionStorage).map(name => ({name, value: sessionStorage.getItem(name)}))
    })
  });
  const origin = results[0]?.result;
  if (!origin || !['https://muse.ai', 'https://www.muse.ai'].includes(origin.origin))
    throw new Error('Tab Muse đã chuyển trang. Mở lại Muse rồi thử lại.');
  if (!cookies.length && !origin.localStorage.length && !origin.sessionStorage.length)
    throw new Error('Chưa tìm thấy phiên Muse. Đăng nhập xong trên tab Muse rồi thử lại.');
  return {cookies, origins:[origin]};
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type !== 'muse-local-collect') return false;
  collectSession(sender).then(session => respond({ok:true, session}))
    .catch(error => respond({ok:false, message: error.message || 'Không đọc được phiên Muse.'}));
  return true;
});
