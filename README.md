# Endfield Asset Updater

终末地资源自动更新与解包服务，导出 MasterData、图片、音频、视频和原始资源，支持断点续传与增量更新。

## 部署

```bash
git clone https://github.com/Deseer/Endfield-Asset-Updater.git
cd Endfield-Asset-Updater
```

然后执行：

```bash
cp .env.example .env
cp config/service.example.json /absolute/path/to/private/service.json
chmod 600 /absolute/path/to/private/service.json
```

在私有 JSON 中填写资源源配置，并在 `.env` 中设置 `ENDFIELD_SERVICE_CONFIG_PATH` 和 `ZMD_DATA_ROOT`。然后启动：

```bash
docker compose up -d --build endfield-service
docker compose logs -f endfield-service
```

运行参数见 [`.env.example`](.env.example) 和 [`config/service.example.json`](config/service.example.json)。
默认输出目录为 `./data`。镜像使用 `linux/amd64`，Apple Silicon 需要 x86_64 模拟支持。
私有配置、CDN 地址及运行数据请勿提交到公开仓库。Bark 通知可通过 `ZMD_BARK_SKILL_ROOT` 选配。

## 来源与许可证

原创代码采用 [MIT License](LICENSE)。解包基于 [EIHRTeam/EndfieldStudio](https://github.com/EIHRTeam/EndfieldStudio)，来源与第三方许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
