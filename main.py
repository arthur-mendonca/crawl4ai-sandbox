import sys
import asyncio
from typing import List, Optional

from crawl4ai import AsyncWebCrawler
from crawl4ai.async_configs import BrowserConfig, CrawlerRunConfig, CacheMode
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

# Ensure a subprocess-friendly event loop on Windows for Playwright
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = FastAPI(title="Crawler API")


class CrawlRequest(BaseModel):
    url: HttpUrl
    css_selector: Optional[str] = None


class CrawlResponse(BaseModel):
    markdown_preview: str
    content_length: int
    images: List[str]
    internal_links: List[str]
    error: Optional[str] = None


async def crawl_url(url: str, custom_selector: Optional[str] = None) -> CrawlResponse:
    print(f"--- Iniciando Crawl Generalista para: {url} ---")
    
    # 1. Configuração do Navegador
    browser_config = BrowserConfig(
        verbose=True,
        headless=True,
        user_agent_mode="random", # Essencial para evitar bloqueios simples
        viewport_width=1280,      # Largura desktop padrão para carregar layout completo
        viewport_height=1000,
    )

    # 2. Lista de Exclusão Universal (Generalista)
    # Remove padrões comuns de anúncios, menus e rodapés em sites de notícias
    general_exclusions = [
        # Estruturais
        "header", "footer", "nav", "aside", ".sidebar", 
        
        # Ads e Marketing
        ".advertisement", ".ad", ".ads", ".banner", ".pub", ".publicidade",
        ".content-interlude", "[id*='google_ads']", ".b-taboola",
        
        # Interação Social e Metadados
        ".share", ".social", ".comments", "#comments", ".meta", ".author-bio",
        ".newsletter", ".subscription", ".paywall-overlay",
        
        # Conteúdo Relacionado (Onde geralmente entra o lixo no meio do texto)
        ".related", ".read-more", ".leia-tambem", ".saiba-mais", ".widget"
    ]

    site_exclusions = [
        ".ads-container", ".ads-placeholder-wrapper", ".ads-placeholder-label",
        ".link-list-wrapper", ".news-block", ".container-mais-lidas",
        ".social-media-lower", ".news-tags", ".breadcrumb", ".container-related-items",
        ".header-related-content", ".container-loader", ".container-action-icons",
        ".styles__RelatedContentNewsStyled-sc-1y91e93-0", ".styles__LinkListWrapper-sc-9ma457-0",
        ".styles__NewsTagsStyled-sc-1u27r72-0", ".styles__ContainerRecirculacaoNoticiaBlockStyled-sc-pb3ng2-0",
        ".container-list-of-lastest", ".items-mais-lidas", ".container-mais-lidas",
        ".styles__SocialMediaLowerStyled-sc-1b6k30l-0", "#tudo-sobre-noticia",
        "[data-fusion-type*='Comentarios']", ".comments", "#comments"
    ]

    # 3. Gerador de Markdown
    # Usamos 'ignore_links=True' para manter o TEXTO do link, mas remover a URL.
    # Isso impede que palavras-chave como "Bets" sumam do texto final.
    md_generator = DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(
            threshold=0.35,
            threshold_type="fixed"
        ),
        content_source="cleaned_html",
        options={
            "ignore_links": True,    # [Texto](url) vira apenas "Texto"
            "ignore_images": True,   # Remove imagens para limpar o texto
            "body_width": 0,         # Texto fluido
            "escape_html": True      # Remove tags HTML residuais
        }
    )

    md_generator_relaxed = DefaultMarkdownGenerator(
        content_source="raw_html",
        options={
            "ignore_links": True,
            "ignore_images": True,
            "body_width": 0,
            "escape_html": True
        }
    )

    # 4. Configuração de Execução (CrawlerRunConfig)
    is_estadao = "estadao.com.br" in url

    run_config = CrawlerRunConfig(
        # SELETOR GENERALISTA:
        # Se o usuário não passar um seletor, tentamos pegar a tag <article> (padrão HTML5)
        # ou <main>, ou classes comuns de conteúdo.
        css_selector=custom_selector or (
            "#content .news-body" if is_estadao
            else "article, main, #content, .news-body, .main-content, .post-content, .entry-content"
        ),
        
        # Exclusão
        excluded_selector=", ".join(general_exclusions + site_exclusions),
        excluded_tags=["script", "style", "noscript", "iframe", "form", "button", "svg"],
        
        # ROLAGEM DE PÁGINA (CRUCIAL PARA EVITAR TRUNCAMENTO):
        # scan_full_page=True faz o crawler rolar a página para carregar conteúdo lazy-load
        scan_full_page=True, 
        scroll_delay=0.5,      # Um leve delay para garantir o render
        wait_for="js:() => document.querySelector('#content .news-body') || document.querySelector('article') || document.querySelector('.news-body')",
        delay_before_return_html=2.0,
        page_timeout=60000,
        
        # Limpeza e Anti-Bot
        remove_overlay_elements=True, # Remove popups de login/paywall
        magic=True,                   # Tenta lidar com consentimentos de cookie
        simulate_user=True,
        override_navigator=True,
        
        # Extração
        markdown_generator=md_generator,
        word_count_threshold=1,       # Mantém parágrafos curtos (comuns em notícias)
        cache_mode=CacheMode.BYPASS
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        try:
            result = await crawler.arun(url=url, config=run_config)
        except Exception as exc:
            return CrawlResponse(
                markdown_preview="",
                content_length=0,
                images=[],
                internal_links=[],
                error=str(exc)
            )

        if not result.success:
            return CrawlResponse(
                markdown_preview="",
                content_length=0,
                images=[],
                internal_links=[],
                error=result.error_message
            )

        # Processamento do Resultado
        final_text = ""
        if result.markdown:
            if hasattr(result.markdown, "fit_markdown") and result.markdown.fit_markdown:
                final_text = result.markdown.fit_markdown
            elif hasattr(result.markdown, "raw_markdown"):
                final_text = result.markdown.raw_markdown
            else:
                final_text = str(result.markdown)

        if not final_text and result.cleaned_html:
            final_text = result.cleaned_html
        if not final_text and result.html:
            final_text = result.html
        
        # Fallback de segurança: Se o seletor generalista falhar e retornar vazio,
        # tentamos pegar o body limpo como última opção.
        if len(final_text.strip()) < 200:
            print("Seletor principal retornou pouco texto. Usando fallback relaxado...")
            relaxed_config = CrawlerRunConfig(
                css_selector=custom_selector,
                excluded_tags=["script", "style", "noscript"],
                remove_overlay_elements=True,
                scan_full_page=True,
                scroll_delay=0.5,
                markdown_generator=md_generator_relaxed,
                word_count_threshold=1,
                cache_mode=CacheMode.BYPASS
            )
            relaxed_result = await crawler.arun(url=url, config=relaxed_config)
            if relaxed_result.success and relaxed_result.markdown:
                if hasattr(relaxed_result.markdown, "raw_markdown"):
                    final_text = relaxed_result.markdown.raw_markdown
                else:
                    final_text = str(relaxed_result.markdown)
            elif relaxed_result.success and relaxed_result.cleaned_html:
                final_text = relaxed_result.cleaned_html

        return CrawlResponse(
            markdown_preview=final_text[:3000], # Preview maior
            content_length=len(final_text),
            images=[],
            internal_links=[],
            error=None
        )

@app.get("/crawl", response_model=CrawlResponse)
async def crawl_endpoint(url: HttpUrl):
    response = await crawl_url(str(url))
    if response.error:
        raise HTTPException(status_code=500, detail=response.error)
    return response


@app.post("/crawl", response_model=CrawlResponse)
async def crawl_endpoint_post(payload: CrawlRequest):
    response = await crawl_url(str(payload.url), payload.css_selector)
    if response.error:
        raise HTTPException(status_code=500, detail=response.error)
    return response


if __name__ == "__main__":
    import uvicorn
    
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        # Cria um novo event loop antes de uvicorn.run()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)