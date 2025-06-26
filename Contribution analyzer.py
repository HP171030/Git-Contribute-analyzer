import os
import subprocess
import flet as ft
from collections import defaultdict
from datetime import datetime, timedelta
import json
import random # 작성자 검열을 위해 필요

ROLLBACK_DAYS_THRESHOLD = 3
ROLLBACK_LINE_THRESHOLD = 50  # 대량 변경 기준 (줄 수)

def get_commit_count(dir_path, update_progress=None):
    """
    Git 저장소의 총 커밋 수를 가져와 진행률 계산에 사용
    """
    try:
        if update_progress:
            update_progress("Get Commit Count...", percent=None, loading_state=True) 
        result = subprocess.run(
            ["git", "-C", dir_path, "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        return int(result.stdout.strip())
    except Exception as e:
        if update_progress:
            # 오류 발생 시 진행률 바 초기화 및 오류 메시지 표시
            update_progress(f"Error getting commit count: {e}", percent=0, loading_state=False)
        return None

def stream_git_log(dir_path):
    """
    `git log`를 한 줄씩 스트리밍으로 읽어오는 제너레이터
    대규모 저장소에서 메모리 사용량을 효율적으로 관리
    """
    process = subprocess.Popen(
        ["git", "-C", dir_path, "log", "--numstat", "--date=short", "--pretty=format:---%an|%ad|%H"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, # 에러 출력을 캡처하여 확인
        cwd=dir_path,
        text=True,
        encoding="utf-8"
    )
    for line in process.stdout:
        yield line.rstrip("\n")
    # 프로세스가 종료될 때까지 기다리고 에러가 있었다면 출력
    stderr_output = process.stderr.read()
    if process.wait() != 0: # 0이 아니면 에러있음
        raise subprocess.CalledProcessError(process.returncode, process.args, stderr=stderr_output)

def get_git_log_details_stream(dir_path, excluded_paths, update_progress=None, total_commit_count=None):
    """
    스트리밍 방식으로 Git 로그를 파싱하여 기여도 및 롤백 정보를 추출
    `total_commit_count`가 제공되면 진행률을 퍼센트로 업데이트
    """
    contributions = defaultdict(lambda: defaultdict(lambda: {"count": 0, "files": defaultdict(int), "lines": 0}))
    file_changes = defaultdict(list)

    current_author, current_date, current_commit = None, None, None
    parsed_commit_count = 0

    try:
        logs_generator = stream_git_log(dir_path)

        for line in logs_generator:
            if line.startswith("---"):
                parsed_commit_count += 1
                parts = line[3:].split("|")
                if len(parts) == 3:
                    current_author, current_date, current_commit = parts
                else: # 예외적인 경우 (커밋 해시가 없는 경우 등)
                    current_author, current_date = parts[0], parts[1]
                    current_commit = None

                if update_progress and total_commit_count:
                    percent = int(parsed_commit_count / total_commit_count * 100)
                    update_progress("Processing commits...", percent, loading_state=True)

            elif line.strip(): # 파일 변경 정보 라인
                parts = line.split("\t")
                if len(parts) == 3:
                    added, deleted, path = parts
                    if not path.endswith(".cs"): # .cs 파일만 필터링
                        continue
                    if any(path.startswith(p.strip()) for p in excluded_paths if p.strip()):
                        continue
                    try:
                        added = int(added)
                        deleted = int(deleted)
                    except ValueError: # 특수값 건너뜀
                        continue

                    month = current_date[:7]
                    try:
                        change_date = datetime.strptime(current_date, "%Y-%m-%d")
                    except ValueError: # 날짜 파싱 오류
                        change_date = None

                    if change_date:
                        file_changes[path].append((change_date, added, deleted, current_author))

                    # 롤백 감지 전에는 일단 모든 변경을 누적
                    contributions[month][current_author]["count"] += 1
                    contributions[month][current_author]["files"][path] += added + deleted
                    contributions[month][current_author]["lines"] += added + deleted

    except subprocess.CalledProcessError as e:
        return {}, f"Git command error: {e.stderr}"
    except Exception as e:
        return {}, f"Error during log parsing: {e}"

    # --- 롤백 탐지 및 기여도 조정 (로그 스트리밍 후 일괄 처리) ---
    rollback_files = set()
    for path, changes in file_changes.items():
        changes.sort(key=lambda x: x[0]) # 날짜 순 정렬
        for i in range(len(changes) - 1):
            date1, added1, deleted1, author1 = changes[i]
            date2, added2, deleted2, author2 = changes[i + 1]
            days_diff = (date2 - date1).days

            if days_diff <= ROLLBACK_DAYS_THRESHOLD:
                # 롤백 했는지? (임의 로직)
                if deleted1 >= ROLLBACK_LINE_THRESHOLD and added2 >= ROLLBACK_LINE_THRESHOLD:
                    rollback_files.add(path)

    # 롤백된 파일에 대한 기여도 조정
    for month, authors in contributions.items():
        for author, data in authors.items():
            for path in rollback_files:
                if path in data["files"]:
                    data["lines"] -= data["files"][path]
                    del data["files"][path]

    return contributions, None

def build_author_button(month, author, data, on_click, get_censored_author_func):
    percent = data["lines"]
    display_author = get_censored_author_func(author)
    return ft.TextButton(
        f"{display_author} ({percent}Lines)",
        on_click=lambda e: on_click(month, author)
    )

def main(page: ft.Page):
    page.title = "Git Contribution Analyzer"
    page.scroll = "adaptive"
    page.window_width = 800
    page.window_height = 700

    path_input = ft.TextField(label="Git Directory", width=700)
    third_party_input = ft.TextField(
        label="Third-party paths to exclude (separated by ',')", width=700,
        hint_text="예: Assets/Photon, Assets/Plugins",
        value="Assets/ThirdParty, Assets/Plugins, Assets/XR" # 기본값 설정
    )
    checkAuthor_input = ft.TextField(
        label="Check Author (comma-separated)",
        hint_text="특정 작성자는 가리지 않고 표시 (예: John Doe, Jane Smith)",
        width=700
    )
    # 리소스 경로 모자이크 처리 체크박스 추가
    mask_paths_checkbox = ft.Checkbox(
        label="Mask Resource Paths in Report",
        value=True, # 기본값은 True로 설정
    )

    analyze_button = ft.ElevatedButton("Start Analysis")
    save_html_button = ft.ElevatedButton("Save as HTML Chart")
    progress_text = ft.Text("Progress : 0%")
    progress_bar = ft.ProgressBar(width=700, value=0)
    status_text = ft.Text()
    detail_text = ft.TextField(
        multiline=True, read_only=True, width=700, height=250,
        border=ft.InputBorder.OUTLINE
    )

    results_column = ft.Column(scroll=ft.ScrollMode.ALWAYS)

    contributions_data = {}
    author_censor_map = {} # 작성자 검열 매핑

    def get_censored_author(author):
        check_authors = [a.strip() for a in checkAuthor_input.value.split(",") if checkAuthor_input.value and a.strip()]
        if check_authors and author not in check_authors:
            if author not in author_censor_map:
                if len(author) > 1:
                    idx = random.randint(0, len(author) - 1)
                    # 랜덤하게 노출, 나머지는 안보이도록 처리
                    keep_indices = sorted(random.sample(range(len(author)), min(random.randint(1,2), len(author))))
                    masked_chars = ['*' for _ in author]
                    for i in keep_indices:
                        masked_chars[i] = author[i]
                    masked = ''.join(masked_chars)
                else:
                    masked = author # 1글자 이름은 마스킹하지 않아요.
                author_censor_map[author] = masked
            return author_censor_map[author]
        else:
            return author

    def get_masked_path(path):
        if not mask_paths_checkbox.value:
            return path # 체크박스가 해제되어 있으면 원본 경로 반환
        
        # 경로에서 한두 글자만 남기고 나머지를 '-' 처리
        parts = path.split('/')
        masked_parts = []
        for part in parts:
            if len(part) <= 2: # 짧은 이름은 모자이크 안 함
                masked_parts.append(part)
            else:
                # 시작과 끝에서 한 글자씩만 남기고 나머지를 '-' 처리
                if len(part) > 2:
                    masked_part = part[0] + '-' * (len(part) - 2) + part[-1]
                else: # 2글자 이하의 경우 전체 마스킹 또는 원본 유지 등 전략 결정
                    masked_part = '-' * len(part)
                masked_parts.append(masked_part)
        return '/'.join(masked_parts)


    def update_progress(message, percent=None, loading_state=False):
        status_text.value = message
        if loading_state:
            if percent is not None:
                progress_bar.value = percent / 100
                progress_text.value = f"Progress: {percent}%"
            else:
                progress_text.value = "Loading..."
        else:
            progress_bar.value = 0
            progress_text.value = ""
        page.update()

    def show_detail(month, author_original):
        detail = contributions_data[month][author_original]
        lines = [f"[{month}] {author_original}의 변경 사항:"]
        lines.append(f"Changed {detail['lines']} Lines")
        sorted_files = sorted(detail["files"].items(), key=lambda item: item[1], reverse=True)
        for file, count in sorted_files:
            display_path = get_masked_path(file) if mask_paths_checkbox.value else file
            lines.append(f"- {display_path}: {count}Lines")
        detail_text.value = "\n".join(lines)
        page.update()

    def on_analyze(e):
        """분석 시작 버튼 클릭 시 호출됩니다."""
        repo_path = path_input.value.strip()
        third_party_paths = [p.strip() for p in third_party_input.value.strip().split(",") if p.strip()]
        author_censor_map.clear()
        
        results_column.controls.clear()
        detail_text.value = ""
        save_html_button.disabled = True
        update_progress("Ready to analyze...", 0, loading_state=False)

        if not os.path.isdir(repo_path):
            status_text.value = "Error: Input Valid Directory"
            page.update()
            return
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            status_text.value = "Error: Not a Git Repository."
            page.update()
            return

        # 1. Git 커밋 수 가져오기
        update_progress("Getting commit count...", percent=None, loading_state=True)
        commit_count = get_commit_count(repo_path, update_progress)
        if commit_count is None:
            status_text.value = "Error: Could not get commit count. Please check Git path."
            page.update()
            return

        # 2. Git 로그 스트리밍 및 기여도 분석
        update_progress("Starting analysis...", 0, loading_state=True)
        nonlocal contributions_data
        contributions_data, error = get_git_log_details_stream(repo_path, third_party_paths, update_progress, commit_count)

        if error:
            status_text.value = f"Analysis Error: {error}"
            page.update()
            return

        # 3. 분석 결과 UI에 표시
        for month in sorted(contributions_data.keys()):
            results_column.controls.append(ft.Text(f"📅 {month}", size=20, weight="bold"))
            authors_row = ft.Row(wrap=True, spacing=10)
            for author, data in sorted(contributions_data[month].items(), key=lambda x: -x[1]["lines"]):
                authors_row.controls.append(build_author_button(month, author, data, show_detail, get_censored_author))
            results_column.controls.append(authors_row)
            results_column.controls.append(ft.Divider())

        update_progress("Analysis Complete!", 100, loading_state=False)
        save_html_button.disabled = False
        page.update()

    def on_save_html_chart(e):
        if not contributions_data:
            status_text.value = "No analysis results to save."
            page.update()
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"git_contributions_monthly_chart_{timestamp}.html"

        # 모든 작성자에 대한 고유 색상 매핑 생성
        all_authors = set()
        for month_data in contributions_data.values():
            for author_name in month_data.keys():
                all_authors.add(author_name)
        
        sorted_all_authors = sorted(list(all_authors))
        
        colors_palette = [
            '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF', '#FF9F40',
            '#E7E9ED', '#8B0000', '#006400', '#483D8B', '#B8860B', '#008B8B',
            '#DC143C', '#FF8C00', '#9400D3', '#00BFFF', '#6B8E23', '#D2B48C',
            '#F0E68C', '#8A2BE2', '#5F9EA0', '#FF1493', '#ADFF2F', '#FA8072',
            '#20B2AA', '#DA70D6', '#7B68EE', '#B0C4DE', '#FFE4B5', '#8B4513'
        ]
        
        author_colors_map = {}
        for i, author in enumerate(sorted_all_authors):
            author_colors_map[author] = colors_palette[i % len(colors_palette)]

        monthly_charts_html = ""
        detailed_sections_html = ""
        javascript_chart_configs = ""

        # 월별 데이터 순회 및 HTML/JS 생성
        for month_idx, month in enumerate(sorted(contributions_data.keys())):
            month_id = f"chart-{month.replace('-', '')}"
            
            current_month_author_lines = defaultdict(int)
            for author, data in contributions_data[month].items():
                current_month_author_lines[author] += data["lines"]
            
            chart_labels = []
            chart_data = []
            chart_colors_for_month = []
            
            sorted_monthly_authors = sorted(current_month_author_lines.items(), key=lambda item: item[1], reverse=True)

            for author, lines in sorted_monthly_authors:
                chart_labels.append(get_censored_author(author))
                chart_data.append(lines)
                chart_colors_for_month.append(author_colors_map[author])

            if chart_data: # 해당 월에 기여 데이터가 있을 때만 차트 섹션 생성
                monthly_charts_html += f"""
        <div class="month-chart-section">
            <h3>📅 {month} Contribution ({sum(chart_data)} Lines)</h3>
            <div class="chart-container-monthly">
                <canvas id="{month_id}"></canvas>
            </div>
        </div>
                """

                javascript_chart_configs += f"""
new Chart(document.getElementById('{month_id}').getContext('2d'), {{
    type: 'pie',
    data: {{
        labels: {json.dumps(chart_labels)},
        datasets: [{{
            data: {json.dumps(chart_data)},
            backgroundColor: {json.dumps(chart_colors_for_month)},
            hoverOffset: 10,
            borderColor: '#fff',
            borderWidth: 2
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
            legend: {{
                position: 'right',
                labels: {{ font: {{ size: 12 }} }}
            }},
            title: {{
                display: true,
                text: '{month} Lines Changed by Author',
                font: {{ size: 16, weight: 'bold' }},
                padding: {{ top: 10, bottom: 20 }}
            }},
            tooltip: {{
                callbacks: {{
                    label: function(context) {{
                        let label = context.label || '';
                        if (label) {{ label += ': '; }}
                        if (context.parsed !== null) {{ label += context.parsed + ' Lines'; }}
                        return label;
                    }}
                }}
            }}
        }}
    }}
}});
"""
        # 월별 상세 기여도 정보 추가
        for month in sorted(contributions_data.keys()):
            current_month_total_lines = sum(data["lines"] for data in contributions_data[month].values())

            detailed_sections_html += f"""
        <div class="month-detail-section">
            <details>
                <summary><h3>📅 {month} Detailed Contributions ({current_month_total_lines} Lines)</h3></summary>
                <div class="author-list-container">
        """
            for author, data in sorted(contributions_data[month].items(), key=lambda x: -x[1]["lines"]):
                display_author_detail = get_censored_author(author)
                
                files_list_html = ""
                if data["files"]:
                    files_list_html += """<p>Files Changed:</p><ul class="file-list">"""
                    sorted_files = sorted(data["files"].items(), key=lambda item: item[1], reverse=True)
                    for file_path, lines_changed in sorted_files:
                        # 파일 경로에도 모자이크 처리 적용
                        display_file_path = get_masked_path(file_path)
                        files_list_html += f"<li>- {display_file_path}: {lines_changed} lines</li>"
                    files_list_html += "</ul>"

                detailed_sections_html += f"""
                    <div class="author-section">
                        <h4>{display_author_detail} ({data['lines']} Lines)</h4>
                        <p>Total Commits: {data['count']}</p>
                        {files_list_html}
                    </div>
                """
            detailed_sections_html += """
                </div>
            </details>
        </div>
        """

        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Git Monthly Contribution Charts</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; line-height: 1.6; color: #333; background-color: #f4f7f6; }}
        h1, h2, h3, h4 {{ color: #333; text-align: center; margin-bottom: 20px; }}
        .header-section {{ background-color: #e0f2f7; padding: 20px; border-radius: 8px; margin-bottom: 30px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }}

        .charts-container {{
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 40px;
            margin-top: 50px;
            padding: 20px;
            background-color: #fff;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .month-chart-section {{
            flex: 0 0 calc(50% - 40px);
            max-width: 400px;
            min-width: 300px;
            box-sizing: border-box;
            text-align: center;
            padding: 20px;
            border: 1px solid #ddd;
            border-radius: 8px;
            background-color: #fcfcfc;
            box-shadow: 0 2px 5px rgba(0,0,0,0.05);
        }}
        .chart-container-monthly {{
            position: relative;
            height: 300px;
            width: 100%;
            margin: 0 auto;
        }}

        .author-details {{
            margin-top: 50px;
            border-top: 1px solid #eee;
            padding-top: 20px;
            background-color: #fff;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            padding: 30px;
        }}
        .month-section {{
            margin-bottom: 30px;
            border: 1px solid #ddd;
            padding: 15px;
            border-radius: 8px;
            background-color: #f9f9f9;
        }}
        .author-section {{
            margin-left: 20px;
            margin-bottom: 15px;
            padding: 10px;
            border: 1px solid #eee;
            border-left: 5px solid #36A2EB;
            border-radius: 5px;
            background-color: #fff;
        }}
        .file-list {{
            list-style-type: none;
            padding-left: 0;
            font-size: 0.9em;
        }}
        .file-list li {{
            margin-bottom: 3px;
            word-wrap: break-word;
        }}
        .file-list li:before {{
            content: "• ";
            color: #36A2EB;
            font-weight: bold;
            display: inline-block;
            width: 1em;
            margin-left: -1em;
        }}
    </style>
</head>
<body>
    <div class="header-section">
        <h1>Git Monthly Contribution Analysis</h1>
        <p style="text-align: center;">Analyzed from: {path_input.value}</p>
        <p style="text-align: center;">Excluded paths: {third_party_input.value or 'None'}</p>
    </div>

    <h2>Monthly Contribution Charts (Lines Changed)</h2>
    <div class="charts-container">
        {monthly_charts_html}
    </div>

    <div class="author-details">
        <h2>Detailed Contributions by Month and Author</h2>
        {detailed_sections_html}
    </div>

    <script>
        {javascript_chart_configs}
    </script>
</body>
</html>
"""
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html_content)
            status_text.value = f"Results saved to {filename}"
        except Exception as ex:
            status_text.value = f"Error saving HTML file: {ex}"
        page.update()

    analyze_button.on_click = on_analyze
    save_html_button.on_click = on_save_html_chart
    save_html_button.disabled = True

    page.add(
        ft.Column([
            ft.Text("📊 Git Contribution Analyzer", size=30, weight="bold"),
            path_input,
            third_party_input,
            checkAuthor_input,
            mask_paths_checkbox, # UI에 체크박스 추가
            ft.Row([analyze_button, save_html_button]),
            status_text,
            progress_bar,
            progress_text,
            ft.Divider(),
            results_column,
            ft.Text("Detail Info", size=20, weight="bold"),
            detail_text
        ], spacing=10)
    )

if __name__ == "__main__":
    ft.app(target=main)