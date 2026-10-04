FROM mcr.microsoft.com/dotnet/sdk:9.0-bookworm-slim AS build
WORKDIR /src
COPY vendor/EndfieldStudio/ ./
RUN dotnet publish AnimeStudio.Endfield.Cli/AnimeStudio.Endfield.Cli.csproj \
    -c Release -r linux-x64 --self-contained false -o /publish \
    -m:1 /p:BuildInParallel=false

FROM mcr.microsoft.com/dotnet/runtime:9.0-bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV TZ=Asia/Shanghai
ADD --checksum=sha256:628963bf2ee9108a97260fa5eef44acd9ec94369b76090a957c9182b3abbb558 https://github.com/sisong/HDiffPatch/releases/download/v5.1.3/hdiffpatch_v5.1.3_bin_linux64.zip /tmp/hdiffpatch.zip
RUN python3 -m zipfile -e /tmp/hdiffpatch.zip /tmp/hdiffpatch \
    && install -m 0755 /tmp/hdiffpatch/linux64/hpatchz /usr/local/bin/hpatchz \
    && /usr/local/bin/hpatchz -v \
    && rm -r /tmp/hdiffpatch /tmp/hdiffpatch.zip
ADD --checksum=sha256:2f98c77f756079f63fbd119939067f1ed461d77e70993bc4cc372736d859c84a https://github.com/vgmstream/vgmstream/releases/download/r2117/vgmstream-linux.zip /tmp/vgmstream.zip
RUN python3 -m zipfile -e /tmp/vgmstream.zip /usr/local/bin \
    && chmod +x /usr/local/bin/vgmstream-cli \
    && rm /tmp/vgmstream.zip
COPY --from=build /publish/ /app/endfield/
COPY LICENSE THIRD_PARTY_NOTICES.md /licenses/
COPY vendor/EndfieldStudio/LICENSE /licenses/EndfieldStudio-LICENSE
COPY scripts/ /app/scripts/
COPY zmd_resource_service/ /app/zmd_resource_service/
RUN chmod +x /app/endfield/endfield-dump /app/scripts/*.sh \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
ENV PATH="/app/endfield:${PATH}"
ENTRYPOINT []
CMD ["endfield-dump", "--help"]
