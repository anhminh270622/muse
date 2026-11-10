(() => {
  if (!['8765','8766'].includes(location.port) || location.pathname !== '/') return;
  document.documentElement.dataset.museConnector = '1';
  let busy = false;
  const update = (message, state) => {
    window.dispatchEvent(new CustomEvent('muse-connector-status', {detail:{message, state}}));
  };
  async function local(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {'X-Muse-Local':'1', 'Content-Type':'application/json'},
      ...(body === undefined ? {} : {body:JSON.stringify(body)}),
      credentials:'same-origin', redirect:'error'
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Tool từ chối kết nối.');
    return result;
  }
  document.addEventListener('click', async event => {
    if (!event.isTrusted || !event.target.closest?.('#connect-browser') || busy) return;
    busy = true;
    try {
      update('Đang kết nối phiên Muse từ trình duyệt này…', 'busy');
      const challenge = await local('/api/browser/challenge');
      if (challenge.protocol !== 'muse-local-bridge-v1') throw new Error('Không nhận diện được Muse Video Local.');
      const result = await chrome.runtime.sendMessage({type:'muse-local-collect'});
      if (!result?.ok) throw new Error(result?.message || 'Không đọc được phiên Muse.');
      await local('/api/browser/connect', {token:challenge.token, session:result.session});
      update('Đã chuyển phiên. Đang kiểm tra đăng nhập bằng trình duyệt ẩn…', 'busy');
      const checked = await local('/api/connection/check', {});
      update(checked.ok ? 'Đã kết nối ô chat Muse. Chưa kiểm chứng tạo/tải video.' :
        'Đã nhận phiên nhưng chưa vào được ô chat Muse. Kiểm tra đăng nhập, xác minh và cấu hình ô nhập.', checked.ok ? 'ok' : 'attention');
    } catch (error) {
      update(error.message || 'Không kết nối được. Tải lại trang và thử lại.', 'error');
    } finally { busy = false; }
  }, true);
})();
