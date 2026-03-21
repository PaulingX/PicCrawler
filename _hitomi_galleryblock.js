"use strict";


var tag_display;

(function(){

var galleries_per_page = 25;
var results_array = {};
var outstanding_requests = {};
var number_of_outstanding_requests = 0;
var nozomi = [];
var tag, area, orderby, orderbykey, language, page_number = 1;
var total_items = 0;

function get_block(url) {
        $.get(url).always(function(data, status) {
                if (status === 'success') {
                        results_array[url] = rewrite_tn_paths(data);
                } else {
                        console.log("failed to $.get(): "+url+"  status: "+status);
                        results_array[url] = '';
                }
                delete outstanding_requests[url];
                --number_of_outstanding_requests;
                put_results_on_page();
        });
}

function put_results_on_page() {
        var datas = [];
        var end = Math.min(nozomi.length, galleries_per_page);
        for (var i = 0; i < end; ++i) {
                var url = '//'+domain+'/'+galleryblockdir+'/'+nozomi[i].toString()+galleryblockextension;
                if (url in results_array) {
                        datas.push(results_array[url]);
                        continue;
                }
                if (!outstanding_requests[url]) {
                        outstanding_requests[url] = 1;
                        ++number_of_outstanding_requests;
                        get_block(url); //calling a function is REQUIRED to give url its own scope
                }
        }
        if (number_of_outstanding_requests) return;
        
        $(function() {
                mark_unread(datas);
                hide_loading();
                $('.gallery-content').html(datas.join(''));
                $('.gallery-content').removeAttr('style');
                moveimages();
                if (/\/date\//.test(window.location.href)) {
                        set_date_published();
                }
                localDates();
                limitLists();
        
                var last_page_number = Math.ceil(total_items/galleries_per_page);
                var base = '/'+[tag, language].join('-');
                if (area) {
                        if (orderbykey) {
                                if (!['popular', 'date'].includes(area)) {
                                        //series/popular/today/female:filming-german
                                        base = '/'+area+'/'+orderby+'/'+orderbykey+base;
                                } else {
                                        //popular/today-czech
                                        base = '/'+orderby+base;
                                }
                        } else {
                                base = '/'+area+base;
                        }
                }
                insert_paging(base, '-', '.html', page_number, last_page_number);

                if ('loading' in HTMLImageElement.prototype) {
                        flip_lazy_images();
                }
        });
}

function fetch_nozomi() {
        var filepath = decodeURIComponent(document.location.href.replace(/.*hitomi\.la\//, ''));
        if (!filepath) {
                tag = 'index';
                language = 'all';
                page_number = 1;
        } else if (/^\?page=\d+$/.test(filepath)) {
                tag = 'index';
                language = 'all';
                
                page_number = parseInt(filepath.replace(/.*\?page=(\d+)$/, '$1'));
                if (!page_number || page_number < 1) return;
        } else {
                if (/\?page=\d+$/.test(filepath)) {
                        page_number = parseInt(filepath.replace(/.*\?page=(\d+)$/, '$1'));
                        if (!page_number || page_number < 1) return;
                }
                
                var elements = filepath.replace(/\.html(?:\?page=\d+)?$/, '').split('-');
                if (elements.length < 2) return;
                while (elements.length > 2) {
                        elements[1] = elements[0] + '-' + elements[1];
                        elements.shift();
                }
                //[series/popular/today/female:filming, german]
                //[popular/today, czech]

                tag = elements[0];
                //series/popular/today/female:filming
                //popular/today
                if (/\//.test(tag)) {
                        var area_elements = tag.split(/\//);
                        //[series, popular, today, female:filming]
                        //[popular, today]  [date, published]
                        if (['popular', 'date'].includes(area_elements[1])) {
                                orderby = area_elements[1];
                                //popular
                                orderbykey = area_elements[2];
                                //today
                                area_elements.splice(1, 2); //delete elements 2 and 3
                                //[series, female:filming]
                        } else if (['popular', 'date'].includes(area_elements[0])) {
                                orderby = area_elements[0];
                                //popular
                                orderbykey = area_elements[1];
                                //today
                        }
                        if (area_elements.length !== 2) return;

                        area = area_elements[0];
                        //series
                        //popular
                        if (!area || /[^A-Za-z0-9_]/.test(area)) return;
        
                        tag = area_elements[1];
                        //female:filming
                        //today
                }

                language = elements[1];
                if (!language || /[^A-Za-z]/.test(language)) return;
        }
        
        tag_display = tag.replace(/(?:fe)?male:/, '');
        if (area === 'popular') {
                tag_display = 'popular ('+tag+')';
        }
        
        var nozomi_address = '//'+[domain, [tag, language].join('-')].join('/')+nozomiextension;
        if (area) {
                nozomi_address = '//'+[domain, area, [tag, language].join('-')].join('/')+nozomiextension;
                if (orderbykey && !['popular', 'date'].includes(area)) { //series/popular/today/female:filming-german
                        nozomi_address = '//'+[domain, area, orderby, orderbykey, [tag, language].join('-')].join('/')+nozomiextension;
                }
        }
        
        var start_byte = (page_number - 1) * galleries_per_page * 4;
        var end_byte = start_byte + galleries_per_page * 4 - 1;

        var xhr = new XMLHttpRequest();
        xhr.open('GET', nozomi_address);
        xhr.responseType = 'arraybuffer';
        xhr.setRequestHeader('Range', 'bytes='+start_byte.toString()+'-'+end_byte.toString());
        xhr.onreadystatechange = function(oEvent) {
                if (xhr.readyState === 4) {
                        if (xhr.status === 200 || xhr.status === 206) {
                                var arrayBuffer = xhr.response; // Note: not oReq.responseText
                                if (arrayBuffer) {
                                        var view = new DataView(arrayBuffer);
                                        var total = view.byteLength/4;
                                        for (var i = 0; i < total; i++) {
                                                nozomi.push(view.getInt32(i*4, false /* big-endian */));
                                        }
                                        total_items = parseInt(xhr.getResponseHeader("Content-Range").replace(/^[Bb]ytes \d+-\d+\//, '')) / 4;
                                        
                                        put_results_on_page();
                                }
                        }
                }
        };
        xhr.send();
}

function set_title() {
        if ('all' === language) {
                if ('index' === tag) {
                        document.title = 'Hitomi.la';
                } else if ('popular' === area) {
                        document.title = 'Popular '+document.title;
                } else if ('date' === area) {
                        document.title = 'Hitomi.la';
                } else if (orderbykey && orderby === 'popular') {
                        document.title = tag_display+' (by popularity) '+document.title;
                } else {
                        document.title = tag_display+' '+document.title;
                }
        } else {
                var our_localname = language;
                if (language_localname.hasOwnProperty(language)) {
                        our_localname = language_localname[language];
                }
                if ('index' === tag) {
                        document.title = our_localname+' '+document.title;
                } else if ('popular' === area) {
                        document.title = our_localname+' (popular) '+document.title;
                } else if ('date' === area) {
                        document.title = our_localname+' '+document.title;
                } else if (orderbykey && orderby === 'popular') {
                        document.title = tag_display+' ('+our_localname+', by popularity) '+document.title;
                } else {
                        document.title = tag_display+' ('+our_localname+') '+document.title;
                }
        }
}

function set_feed_url() {
        var pathname = decodeURIComponent(document.location.pathname);
        
        if (!/\/(?:date|popular)\//.test(pathname)) {
                var feedurl = pathname.replace(/\.html$/, '.atom');
                if (pathname === '/') {
                        feedurl = '/index'+separator+language+'.atom';
                }
                $(function() {
                        $('#feedurl').attr('href', feedurl);
                        $('.rss-icon').show();
                });
        }
}

function fetch_languages() {
        var pathname = decodeURIComponent(document.location.pathname);
        
        var term = pathname.replace(/^\//, '').replace(/(.+)-[^-]+\.html$/, '$1');
        if (term === 'index') {
                term = '';
        }
        $(function() {
                var html = '<li><a href="/'+term+separator+'all'+extension+'">(all)</a></li>';
                if (!term.length) {
                        html = '<li><a href="/">(all)</a></li>';
                }
                $('#lang-list').html(html);
        });
        
        get_index_version('languagesindex').then((string) => {
                languages_index_version = string;
        
                var key = hash_term(term);
                const field = 'languages';
                
                get_node_at_address(field, 0).then((node) => {
                        if (!node) {
                                return;
                        }
                        
                        B_search(field, key, node).then((data) => {
                                if (!data) {
                                        return;
                                }
                                
                                $(function() {
                                        var [offset, length] = data;
                                        var mask = offset.toString(2).split('');
                                        for (var i = 0; i < 52; i++) {
                                                var index = mask.length - i - 1;
                                                if (bitnumber_language.hasOwnProperty(i) && mask.length > i && mask[index] === '1') {
                                                        var language = bitnumber_language[i];
                                                        var localname = language;
                                                        if (language_localname.hasOwnProperty(language)) {
                                                                localname = language_localname[language];
                                                        }
                                                
                                                        var url = '/' + term + separator + language + extension;
                                                        if (!term.length) {
                                                                url = '/index' + separator + language + extension;
                                                        }
                                                        $('#lang-list').append('<li><a href="'+url+'">'+localname+'</a></li>');
                                                }
                                        }
                                });
                        });
                });
        });
}

function fetch_nozomiurl() {
        if (!area) {
                return;
        }
        
        var pathname = decodeURIComponent(document.location.pathname);
        
        //series/popular/today/female:filming
        //popular/today
        var term = pathname.replace(/^\//, '').replace(/-[^-]+\.html$/, '').replace(/\/(?:date|popular)\/[^\/]+/, '').replace(/\/\w+:/, '/');

        get_index_version('nozomiurlindex').then((string) => {
                nozomiurl_index_version = string;

                var key = hash_term(term);
                const field = 'nozomiurl';
                
                get_node_at_address(field, 0).then((node) => {
                        if (!node) {
                                return;
                        }
                        
                        B_search(field, key, node).then((data) => {
                                if (!data) {
                                        return;
                                }
                                
                                var [offset, length] = data;
                                if (length > 10000 || length <= 0) {
                                        console.error("length "+length+" is too long");
                                        return;
                                }
                                
                                var url = '//'+domain+'/nozomiurlindex/'+field+'.'+nozomiurl_index_version+'.data';
                                get_url_at_range(url, [offset, offset+length-1]).then((inbuf) => {
                                        if (!inbuf) {
                                                return;
                                        }
                
                                        var view = new DataView(inbuf.buffer);
                                        var string = new TextDecoder().decode(inbuf);

                                        $(function() {
                                                $('#nozomiurl').attr('href', string);
                                                $('#nozomiurl').html(tag_display + $('#nozomiurl').html());
                                                $('#nozomi-link').show();
                                        });
                                });
                        });
                });
        });
}

function set_query_input_value() {
        const pathname = decodeURIComponent(document.location.pathname);
        let terms = [];
        
        if (tag !== 'index' && area !== 'popular' && area !== 'date') {
                if (!/:/.test(tag)) {
                        terms.push(area+':'+tag.replace(/ /g, '_'));
                } else {
                        terms.push(tag.replace(/ /g, '_'));
                }
        }
        
        if (language !== 'all') {
                terms.push('language:'+language);
        }

        let match = pathname.match(/\/(date|popular)\/([0-9a-z]+)/);
        if (match) {
                if (match[1] === 'date' && match[2] === 'published') {
                        terms.push('orderby:datepublished');
                } else {
                        terms.push('orderby:'+match[1]);
                        terms.push('orderbykey:'+match[2]);
                }
        }
        
        if (terms.length) {
                $('#query-input').val(terms.join(' ')+' ');
        }
        
        return terms;
}

$(function() {
        let header_text = tag === 'index' ? 'Recently Added' : tag_display;
        if (area === 'date') {
                header_text = 'Recently Published';
        }
        $('#artistname').html(header_text);
        
        start_loading_timer();
        
        const orderbydropdown = document.getElementById("orderbydropdown");
        if (orderbydropdown) {
                orderbydropdown.value = orderbykey ? orderbykey : 'date_added';
                
                orderbydropdown.addEventListener("change", function() {
                        let go_to = function(url) {
                                orderbydropdown.selectedIndex = 0;
                                window.location.href = url;
                        };
                        
                        let url = decodeURIComponent(document.location.pathname).replace(/^\/index/, '').replace(/\/(?:date|popular)\/[^\/]+\//, '/').replace(/\/(?:date|popular)\/[^-]+/, '');
                        
                        if (orderbydropdown.value) {
                                if (orderbydropdown.value === 'date_added') {
                                        if (/^-/.test(url)) {
                                                go_to('/index'+url);
                                        } else {
                                                go_to(url);
                                        }
                                } else if (orderbydropdown.value === 'random') {
                                        go_to('/search.html?'+encodeURIComponent([set_query_input_value().filter(term => !/^(?:sort|order)by(?:key|direction)?:/.test(term)).join(' '), 'orderbydirection:random'].join(' ')));
                                } else {
                                        let orderbykey = orderbydropdown.value, orderby = 'popular';
                                        if (orderbykey === 'published') {
                                                orderby = 'date';
                                        }
                                        
                                        if (!url || url === '/') {
                                                go_to('/'+orderby+'/'+orderbykey+'-all.html');
                                        } else if (/^-/.test(url)) {
                                                go_to('/'+orderby+'/'+orderbykey+url);
                                        } else {
                                                let elements = url.split('/');
                                                elements.splice(2, 0, orderby, orderbykey);
                                                go_to(elements.join('/'));
                                        }
                                }
                        }
                });
        }
});
fetch_nozomi();
set_title();
set_feed_url();
fetch_languages();
fetch_nozomiurl();
set_query_input_value();

})();
