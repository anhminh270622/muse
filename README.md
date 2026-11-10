# Muse Video Local

Tool local nhập kịch bản theo cảnh, điều khiển Muse bằng Chromium luôn headless, lưu tiến độ SQLite, nhập/tải clip và ghép MP4 dọc bằng FFmpeg.

## Chạy

Yêu cầu Python 3.12+, FFmpeg/FFprobe và thư viện hệ thống chạy Chromium.

```bash
cd /home/dvs-minhpa/Documents/my-project/muse-video-local
./run.sh
```

Mở http://127.0.0.1:8765. Đổi port bằng `MUSE_PORT=8766 ./run.sh`.
Server chỉ bind localhost. Không public server hoặc proxy ra Internet: ứng dụng không có hệ thống tài khoản.

## Tình trạng thực tế

- Đã có giao diện, DB, lưu tiến độ, nhập clip, ghép video, adapter Playwright headless.
- Đã xác minh bằng tài khoản Muse thật: gửi prompt, tải clip 10 giây có âm thanh từ tin trả lời, và ghép 6 clip thành video 60 giây. Selector video được giới hạn vào thẻ đính kèm trong hội thoại để loại avatar Muse. Kiểm tra ô chat riêng vẫn chỉ xác nhận đăng nhập; giao diện Muse thay đổi có thể cần cập nhật selector.
- Có nút kết nối trình duyệt qua tiện ích local. Cần cài tiện ích vào hồ sơ Chrome của bạn. Không tự động mở cửa sổ, không yêu cầu nhập mật khẩu trong ứng dụng.
- Chưa có AI tự viết/chia cảnh, TTS, nhạc nền, phụ đề hoặc đăng YouTube. Đầu vào hiện tại là kịch bản đã chia cảnh bằng dòng trống. Có chế độ nối cảnh bằng khung hình cuối: tool lấy frame cuối của clip trước, tải ảnh vào Muse và yêu cầu clip sau bắt đầu từ chính ảnh đó. Vẫn cần xem lại kết quả vì model có thể tạo sai lệch.

## Kết nối trình duyệt đã đăng nhập (khuyến nghị)

Chỉ cần cài tiện ích local một lần, không xuất cookie và không cần API key:

1. Trong chính hồ sơ Chrome đang đăng nhập Muse, mở `chrome://extensions`, bật **Developer mode**.
2. Bấm **Load unpacked**, chọn thư mục `/home/dvs-minhpa/Documents/my-project/muse-video-local/browser-extension` (hoặc tải ZIP từ giao diện, giải nén rồi chọn thư mục đó).
3. Giữ tab Muse đã đăng nhập mở trong hồ sơ này. Tải lại http://127.0.0.1:8765/ và bấm **Kết nối trình duyệt đã đăng nhập**. Tiện ích hiện hỗ trợ cổng 8765 và 8766.
4. Tool nhận phiên rồi kiểm tra ô chat bằng Chromium headless. **Đã nhận phiên** khác với **đã xác minh đăng nhập**; chỉ khi tìm thấy ô chat mới báo kết nối. Việc tạo và tải video vẫn cần thử riêng.

Tiện ích chỉ có quyền cookies/scripting trên `muse.ai` và `www.muse.ai`. Chọn tab Muse được dùng gần nhất, đọc cookies trong đúng cookie store, localStorage và sessionStorage của tab đó. Không mở hoặc điều khiển tab Muse, không đổi trang, không đọc phiên của website khác. Chỉ chuyển phiên khi người dùng bấm nút thật; dữ liệu đi từ content script cô lập sang server localhost với mã dùng một lần, không qua JS của trang và không gửi lên bên thứ ba. Trang chỉ nhận trạng thái. Cookie phân vùng và IndexedDB chưa được tiện ích xuất; tài khoản phụ thuộc các cơ chế này có thể cần bổ sung hỗ trợ.

Mỗi khi đăng nhập lại, đổi tài khoản hoặc phiên hết hạn, bấm kết nối lại. Snapshot phiên được cập nhật sau mỗi lượt chạy trong `data/runtime-session.json` (quyền 0600), để cookie dạng session và cookie đã làm mới không bị mất khi đóng trình duyệt ẩn. Tool nhập snapshot mới một lần và giữ cookies đã làm mới trong hồ sơ headless cho những lượt chạy sau. Không tự bật cửa sổ dù kết nối thất bại. Cài tiện ích bằng tay là bước Chrome yêu cầu; tool không tự sửa cài đặt Chrome hoặc khởi động lại trình duyệt của bạn.

## Nhập file phiên dự phòng

1. Dùng phiên của chính bạn từ trình duyệt Muse đã đăng nhập. Trong giao diện chọn file JSON cookies xuất từ trình duyệt hoặc file Playwright `storageState` có `cookies` và `origins`. File cookies phải có `name`, `value`, `domain`, `path`. Đây là thông tin đăng nhập: chỉ chọn file trên máy, không gửi vào chat.
2. Tool chỉ giữ cookies thuộc Muse/Meta, lưu tại `data/session.json` (quyền 0600), hồ sơ Chromium riêng tại `data/browser-profile`. Không đưa data vào Git. Mỗi lần chạy luôn có `headless=True`.
3. Nhập đúng URL Muse và bấm Kiểm tra. Nếu thấy màn hình đăng nhập, captcha hoặc xác minh, tool dừng; bạn cần hoàn thành xác minh bằng trình duyệt của mình và nhập lại phiên. Cookie có thể không đủ nếu phiên bị ràng buộc với thiết bị; cần kiểm chứng trên tài khoản thực tế.
4. Thử project chỉ có một cảnh trước. Cấu hình selector nếu giao diện Muse khác. Adapter hỗ trợ URL HTTPS trực tiếp và blob tải được bằng trình duyệt. Nếu video dùng MSE/stream hoặc nút tải đặc biệt, cần hiệu chỉnh adapter; không báo thành công giả.
5. Adapter đánh dấu các thẻ video đính kèm cũ trong hội thoại và chỉ nhận video kết quả mới; bỏ qua avatar và chờ URL hội thoại mới sẵn sàng trước khi tiếp tục. URL nguồn được đọc ngay trong phiên trình duyệt, không gọi endpoint nội bộ đoán trước. Clip tải về được FFprobe xác minh.

Không dùng profile Chrome cá nhân mặc định đang mở; không có chế độ headed hoặc fallback bật cửa sổ.

## Tạo video

## Gửi video qua Telegram

- Mở @BotFather trong Telegram, tạo bot bằng `/newbot`, rồi sao chép Bot Token.
- Mở cuộc trò chuyện với bot và gửi `/start`. Nhập chat ID cá nhân; để gửi vào nhóm/kênh, thêm bot vào đích đó và nhập `@username` (hoặc ID số nếu nhóm/kênh riêng tư).
- Trong giao diện local, dán token, nhập chat ID và bấm **Lưu và xác minh bot**. Token được ghi riêng tại `data/telegram.json` với quyền `0600`; API giao diện không trả token về.
- Bấm **Gửi Telegram** ở project đã có MP4 để gửi video trực tiếp bằng Telegram Bot API. Đây là thao tác thủ công, không tự gửi khi render hoàn tất. Bot API hiện giới hạn video tải lên ở 50 MB; video lớn hơn cần nén hoặc máy chủ Bot API cục bộ.

- Mặc định bật **Nối cảnh theo khung hình cuối** cho project mới. Cảnh đầu tạo từ văn bản; từ cảnh thứ hai, tool đính kèm ảnh cuối của cảnh trước và yêu cầu tiếp tục chuyển động, không thiết lập lại góc máy/hành động. Mỗi cảnh có ảnh tham chiếu được tạo trong hội thoại riêng để không dùng nhầm video/ảnh từ bản cũ trong lịch sử. Tool so sánh ảnh tham chiếu với khung đầu của clip mới bằng SSIM; dưới ngưỡng 0,85 sẽ lưu clip ở trạng thái cần sửa và dừng trước cảnh tiếp theo. Đây là phép so sánh khung hình, không bảo đảm nội dung/chuyển động hoàn hảo. Bỏ chọn nếu muốn các clip độc lập.
- Viết 12–18 cảnh, mỗi cảnh một đoạn, cách bằng dòng trống. Muse được yêu cầu khoảng 10 giây mỗi cảnh; thời lượng cuối đo từ clip thực tế.
- Bấm Tạo project, xem cảnh rồi Chạy Muse. Một worker xử lý tuần tự. Worker gửi phong cách chung kèm từng cảnh.
- Khi tool dừng sau khi gửi prompt hoặc khởi động lại, cảnh chuyển `uncertain`. Kiểm tra hội thoại, nhập clip đã tạo hoặc bấm Cho phép tạo lại sau khi đối chiếu. Không tự gửi lại cảnh chưa rõ kết quả.
- Có thể nhập clip Muse đã tải thủ công cho từng cảnh rồi bấm Ghép clip đã nhập để dùng chức năng dựng ngay cả khi adapter chưa tương thích.
- FFmpeg chuẩn hóa 1080×1920, 30 fps, H.264/AAC stereo 48 kHz, giữ khung hình bằng padding, thêm khoảng lặng cho clip không có audio, rồi ghép đúng thứ tự. Audio gốc của clip được giữ; chưa có mix nhạc/giọng đọc xuyên suốt.
- Dừng hàng đợi dừng chờ ở điểm kiểm tra; không hủy tác vụ Muse đã gửi. Tải file đang chạy và bước FFmpeg có thể cần hoàn thành trước khi worker dừng.
- Tiếp tục project giữ clip hoàn thành và chỉ xử lý cảnh còn thiếu. Tạo lại một cảnh làm mất hiệu lực MP4 cuối cũ. Nếu project bật nối cảnh, các cảnh phía sau cũng được đưa về hàng đợi để tạo lại theo ảnh tham chiếu mới; không dùng lại clip đã phụ thuộc khung hình cũ. Thay một clip bằng file nhập thủ công cũng làm các cảnh sau cần tạo lại.

## Kiểm tra

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests dùng DB tạm và Chromium headless để xác minh tải blob video; kiểm tra phục hồi sau crash, chặn gửi lại không rõ kết quả, giới hạn truy cập local và ghép clip thực bằng FFmpeg. Có kiểm thử tiện ích thật trong Chromium headless với phiên và tab Muse giả lập, không tiêu hao hạn mức Muse.


## Ảnh mở đầu và hậu kỳ tùy chọn

- Nếu có `data/<project-id>/opening-frame.png`, worker dùng ảnh đó làm khung đầu cảnh 1 và kiểm tra độ khớp, thay vì tạo cảnh mở đầu chỉ từ văn bản. Chuẩn hóa ảnh về 9:16 trước khi chạy; số/đặc điểm nhân vật có thể chuẩn bị ngay trong ảnh này. Các cảnh sau vẫn dùng frame cuối cảnh trước.
- Nếu có `data/<project-id>/postproduction.json`, worker tự chạy `postprocess.finish_episode` sau khi ghép. Hỗ trợ `memory_flashes` (các đoạn 0,03–0,5 giây từ `source_project_id`/`source_scene`/`source_at` đặt ở `at`), `system_voice` (lời `spoken`, mốc `at`, `display_text: null`) và khoảng im lặng `silence` (`from`/`until`). Không chèn chữ hệ thống lên hình.
- Giọng hệ thống dùng filter `flite` của FFmpeg tại máy, không cần API key. FFmpeg cần được build kèm libflite. Text được đọc từ file để không biến nội dung lời nói thành biểu thức filter.
- Hậu kỳ giữ nguyên thời lượng, lưu bản ghép gốc tại `output-base.mp4`, lời nói tại `system-voice.txt`/`system-voice.wav`, cùng `postproduction-report.json`. Các clip Muse gốc không bị sửa. Nếu hậu kỳ lỗi, dùng lại các clip đã tải để chạy lại; không gửi lại prompt Muse.
