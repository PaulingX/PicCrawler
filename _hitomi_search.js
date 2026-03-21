"use strict";

function replace_smart_quotes(s) { //https://stackoverflow.com/a/10726800
        return s.replace(/[“”‘’\–\—]/g, function (m) {
                return {
                        '“': '"',
                        '”': '"',
                        '‘': "'",
                        '’': "'",
                        '–': '-',
                        '—': '-',
                }[m];
        });
}

function encode_search_query_for_url(s) {
        return s.replace(/[ \/\.]/g, function (m) {
                return {
                        ' ': '_',
                        '/': 'slash',
                        '.': 'dot',
                }[m];
        });
}

function handle_keyup_in_search_box() {
        clear_page();
        
        var query = replace_smart_quotes($('#query-input').val());
        if (!query || !query.length || query.match(/^\s*$/) || query.match(/\s$/) || query === '-') {
                ++search_serial;
                hide_search_button_spinner();
                return;
        }
        
        show_search_button_spinner();

        query = query.toLowerCase();
        var terms = query.split(/\s+/);
        var search_term = terms[terms.length-1];
        search_term = search_term.replace(/^-/, '');
        
        get_suggestions_for_query(search_term, ++search_serial).then((r) => {
                let [results, results_serial] = r;
                
                if (search_serial !== results_serial) return; //outdated request
                
                hide_search_button_spinner();

                $.each(results, function(index, result) {
                        to_page(result, search_term);
                });
                update_search_result_selection();
        
                // highlight query
                let clipped_query = search_term.replace(/_/g, ' ');
                if (search_term.indexOf(':') > -1) {
                        const sides = clipped_query.split(/:/);
                        clipped_query = sides[1];
                }
                $(".search-result").each(function() {
                        var result = $(this).html();
                        var re = new RegExp(clipped_query, 'gi');
                        var final_str = result.replace(re, function(str) {return '<strong>'+str+'</strong>'});
                        $(this).html(final_str);
                });
        
                $('#query-input').parent().addClass("active"); //fix for clicking on a suggestion hides search-suggestions permanently (20141011)
        });
}

$(function() {
        $('#query-input').keydown(function(e) { //prevent movement to the front and end of the input when they press up or down
                if (38 === e.keyCode || 40 == e.keyCode) {
                        return false;
                }
        });
        $('#query-input').keyup(function(e) {
                if (13 === e.keyCode) { //enter
                        if ($(this).parent().hasClass("active") && $(".search-result").length) {
                                var clicked = false;
                                $(".search-result").each(function(index) {
                                        if ($(this).parent().parent().hasClass("selected")) {
                                                $(this).click();
                                                clicked = true;
                                        }
                                });
                                if (!clicked) { //no search result was highlighted
                                        $('#search-button').click();
                                }
                        } else {
                                $('#search-button').click();
                        }
                        return;
                } else if (38 === e.keyCode) { //up
                        --search_result_index;
                        if (search_result_index < 0) {
                                search_result_index = 0;
                        }
                        update_search_result_selection();
                        return;
                } else if (40 === e.keyCode) { //down
                        ++search_result_index;
                        if (search_result_index >= $(".search-result").length) {
                                search_result_index = $(".search-result").length-1;
                        }
                        update_search_result_selection();
                        return;
                } else if (32 === e.keyCode) { //space
                        ++search_serial;
                        hide_search_button_spinner();
                        clear_page();
                        return;
                } else if (16 === e.keyCode) { //shift (20241110)
                        return;
                }
                
                handle_keyup_in_search_box();
        });

        $('#query-input').on('focus', function(event) {
                $(this).parent().addClass("active")
        });

        $(document).on('click', function(event) {
                if (!$(event.target).closest('#query-input').length) {
                        $('#query-input').parent().removeClass("active");
                }
        });
        
        $('#search-button').on('click', function(event) {
                var query = replace_smart_quotes($('#query-input').val());
                query = query.toLowerCase();
                query = query.trim();
                if (!query.length) return;
                
                ++search_serial;
                hide_search_button_spinner();
                
                if (!/\s/.test(query) && query.lastIndexOf('-', 0) !== 0 && query.indexOf(':') > -1) { //one word and doesn't start with - and includes :
                        query = query.replace(/_/g, ' ');
                        
                        var sides = query.split(/:/);
                        var ns = sides[0];
                        var tag = sides[1];
                        
                        show_search_button_spinner();
                        
                        get_suggestions_for_query(query).then((r) => {
                                let [results] = r;
                                
                                hide_search_button_spinner();
                                
                                var found_exact_match = false;
                                
                                $.each(results, function(i, result) { //this assumes that only one result matches exactly (20141011)
                                        if (ns === result.n && tag === result.s) {
                                                if (result.u !== undefined) {
                                                        document.location = result.u;
                                                }
                                                found_exact_match = true;
                                        }
                                });
                                
                                if (!found_exact_match) {
                                        document.location = '/search.html?'+encodeURIComponent(query);
                                }
                        });
                } else {
                        document.location = '/search.html?'+encodeURIComponent(query);
                }
         });
});


function clear_page() {
        $('#search-suggestions').html('');
        search_result_index = -1;
}

function update_search_result_selection() {
        $(".search-result").each(function(index) {
                $(this).parent().parent().removeClass("selected");
                if (index === search_result_index) {
                        $(this).parent().parent().addClass("selected");
                }
        });
}

function to_page(result, term) {
        var topdiv = $('<li>', {
                'class': 'search-suggestion'
        });
        
        topdiv.append(
                $('<a>', {
                        html: '<span class="search-result">' + result.s + '</span>'
                                +(result.n !== undefined ? '<span class="search-ns"> (' + result.n + ')</span>' : ''),
                        'class': 'search-suggestion_string',
                        href: '#'
                }).bind('click', function(event) {
                        event.preventDefault();
                        
                        var underscored = result.s.replace(/\s/g, '_');
                        var namespaced = result.n+':'+underscored;
                        
                        var pos = $('#query-input').getCursorPosition();
                        var query_text = $('#query-input').val();
                        var length = term.length;
                        
                        $('#query-input').val( query_text.splice(pos-length, length, namespaced) + ' ' );
                        $('#query-input').focus();
                        clear_page();
                })
        );
        if (result.t !== undefined) {
                topdiv.append(
                        $('<div>', {
                                html: result.t,
                                'class': 'search-suggestion_total'
                        })
                );
        }
        
        $('#search-suggestions').append(topdiv);
}



function get_url_at_range(url, range) {
        return new Promise((resolve, reject) => {
                retry(() => {
                        return new Promise((resolve, reject) => {
                                var xhr = new XMLHttpRequest();
                                xhr.open('GET', url);
                                xhr.responseType = 'arraybuffer';
                                xhr.setRequestHeader('Range', 'bytes='+range[0].toString()+'-'+range[1].toString());
                                xhr.onreadystatechange = function(oEvent) {
                                        if (xhr.readyState === 4) {
                                                if (xhr.status === 200 || xhr.status === 206) {
                                                        resolve(new Uint8Array(xhr.response));
                                                } else {
                                                        reject(new Error(`get_url_at_range(${url}, ${range}) failed, xhr.status: ${xhr.status}`));
                                                }
                                        }
                                };
                                xhr.send();
                        });
                }).then(resolve).catch(console.error);
        });
}

var decode_node = function(data) {
        let node = {
                keys: [],
                datas: [],
                subnode_addresses: [],
        };
        
        
        let view = new DataView(data.buffer);
        let pos = 0;
        
        
        const number_of_keys = view.getInt32(pos, false /* big-endian */);
        pos += 4;

        let keys = [];
        for (let i = 0; i < number_of_keys; i++) {
                const key_size = view.getInt32(pos, false /* big-endian */);
                if (!key_size || key_size > 32) {
                        console.error("fatal: !key_size || key_size > 32");
                        return;
                }
                pos += 4;
                
                keys.push(data.slice(pos, pos+key_size));
                pos += key_size;
        }


        const number_of_datas = view.getInt32(pos, false /* big-endian */);
        pos += 4;

        let datas = [];
        for (let i = 0; i < number_of_datas; i++) {
                const offset = view.getUint64(pos, false /* big-endian */);
                pos += 8;
                
                const length = view.getInt32(pos, false /* big-endian */);
                pos += 4;

                datas.push([offset, length]);
        }
        
        
        const number_of_subnode_addresses = B+1;
        let subnode_addresses = [];
        for (let i = 0; i < number_of_subnode_addresses; i++) {
                let subnode_address = view.getUint64(pos, false /* big-endian */);
                pos += 8;
                
                subnode_addresses.push(subnode_address);
        }
        
        
        node.keys = keys;
        node.datas = datas;
        node.subnode_addresses = subnode_addresses;
        
        return node;
};

var B_search = function(field, key, node, serial) {
        return new Promise((resolve, reject) => {
                let compare_arraybuffers = function(dv1, dv2) {
                        const top = Math.min(dv1.byteLength, dv2.byteLength);
                        for (let i = 0; i < top; i++) {
                                if (dv1[i] < dv2[i]) {
                                        return -1;
                                } else if (dv1[i] > dv2[i]) {
                                        return 1;
                                }
                        }
                        return 0;
                };
        
                let locate_key = function(key, node) {
                        let cmp_result = -1;
                        let i;
                        for (i = 0; i < node.keys.length; i++) {
                                cmp_result = compare_arraybuffers(key, node.keys[i]);
                                if (cmp_result <= 0) {
                                        break;
                                }
                        }
                        return [!cmp_result, i];
                };
        
                let is_leaf = function(node) {
                        for (let i = 0; i < node.subnode_addresses.length; i++) {
                                if (node.subnode_addresses[i]) {
                                        return false;
                                }
                        }
                        return true;
                };
                
                
                if (!node) {
                        resolve(false);
                        return;
                }


                if (!node.keys.length) { //special case for empty root
                        resolve(false);
                        return;
                }
        
                let [there, where] = locate_key(key, node);
                if (there) {
                        resolve(node.datas[where]);
                        return;
                } else if (is_leaf(node)) {
                        resolve(false);
                        return;
                }
                
                if (node.subnode_addresses[where] == 0) {
                        console.error('non-root node address 0');
                        resolve(false);
                        return;
                }
        
                //it's in a subnode
                get_node_at_address(field, node.subnode_addresses[where], serial).then((node) => {
                        B_search(field, key, node, serial).then(resolve);
                });
        });
};

var get_node_at_address = function(field, address, serial) {
        return new Promise((resolve, reject) => {
                if (serial && serial !== search_serial) { //outdated request
                        resolve();
                        return;
                }
                
                let url = '//'+domain+'/'+index_dir+'/'+field+'.'+tag_index_version+'.index';
                if (field === 'galleries') {
                        url = '//'+domain+'/'+galleries_index_dir+'/galleries.'+galleries_index_version+'.index';
                } else if (field === 'languages') {
                        url = '//'+domain+'/'+languages_index_dir+'/languages.'+languages_index_version+'.index';
                } else if (field === 'nozomiurl') {
                        url = '//'+domain+'/'+nozomiurl_index_dir+'/nozomiurl.'+nozomiurl_index_version+'.index';
                }
                get_url_at_range(url, [address, address+max_node_size-1]).then((nodedata) => {
                        if (nodedata) {
                                resolve(decode_node(nodedata));
                        } else {
                                resolve();
                        }
                });
        });
};

var get_suggestions_from_data = function(field, data) {
        return new Promise((resolve, reject) => {
                if (!data) {
                        resolve([]);
                        return;
                }
                
                let url = '//'+domain+'/'+index_dir+'/'+field+'.'+tag_index_version+'.data';
                let [offset, length] = data;
                if (length > 10000 || length <= 0) {
                        console.error("length "+length+" is too long");
                        resolve([]);
                        return;
                }
                get_url_at_range(url, [offset, offset+length-1]).then((inbuf) => {
                        if (!inbuf) {
                                resolve([]);
                                return;
                        }
                        
                        let suggestions = [];
                        
                        let pos = 0;
                        let view = new DataView(inbuf.buffer);
                        let number_of_suggestions = view.getInt32(pos, false /* big-endian */);
                        pos += 4;
                        if (number_of_suggestions > 100 || number_of_suggestions <= 0) {
                                console.error("number_of_suggestions "+number_of_suggestions+" is too long");
                                resolve([]);
                                return;
                        }
                        
                        let decoder = new TextDecoder('utf-8');
                        for (let i = 0; i < number_of_suggestions; ++i) {
                                let ns = '';
                                let top = view.getInt32(pos, false /* big-endian */);
                                pos += 4;
                                for (let c = 0; c < top; c++) {
                                        ns += String.fromCharCode(view.getUint8(pos, false /* big-endian */));
                                        pos += 1;
                                }
                                
                                top = view.getInt32(pos, false /* big-endian */);
                                pos += 4;
                                let tagbuf = new Uint8Array(top);
                                for (let c = 0; c < top; c++) {
                                        tagbuf[c] = view.getUint8(pos, false /* big-endian */);
                                        pos += 1;
                                }
                                let tag = decoder.decode(tagbuf.buffer);
                                
                                let count = view.getInt32(pos, false /* big-endian */);
                                pos += 4;
                                
                                var tagname = sanitize(tag);
                                var url = '/'+ns+'/'+tagname+separator+'all'+separator+'1'+extension;
                                if (ns === 'female' || ns === 'male') {
                                        url = '/tag/'+ns+':'+tagname+separator+'all'+separator+'1'+extension;
                                } else if (ns === 'language') {
                                        url = '/index-'+tagname+separator+'1'+extension;
                                }
                                suggestions.push({
                                        s: tag,
                                        t: count,
                                        u: url,
                                        n: ns
                                });
                        }
                
                        resolve(suggestions);
                });
        });
};

var get_galleryids_from_data = function(data) {
        return new Promise((resolve, reject) => {
                if (!data) {
                        resolve([]);
                        return;
                }
                
                let url = '//'+domain+'/'+galleries_index_dir+'/galleries.'+galleries_index_version+'.data';
                let [offset, length] = data;
                if (length > 100000000 || length <= 0) {
                        console.error("length "+length+" is too long");
                        resolve([]);
                        return;
                }
                get_url_at_range(url, [offset, offset+length-1]).then((inbuf) => {
                        if (!inbuf) {
                                resolve([]);
                                return;
                        }
                        
                        let galleryids = [];
                        
                        let pos = 0;
                        let view = new DataView(inbuf.buffer);
                        let number_of_galleryids = view.getInt32(pos, false /* big-endian */);
                        pos += 4;
                        
                        let expected_length = number_of_galleryids * 4 + 4;
                        
                        if (number_of_galleryids > 10000000 || number_of_galleryids <= 0) {
                                console.error("number_of_galleryids "+number_of_galleryids+" is too long");
                                resolve([]);
                                return;
                        } else if (inbuf.byteLength !== expected_length) {
                                console.error("inbuf.byteLength "+inbuf.byteLength+" !== expected_length "+expected_length);
                                resolve([]);
                                return;
                        }
                        
                        for (let i = 0; i < number_of_galleryids; ++i) {
                                galleryids.push(view.getInt32(pos, false /* big-endian */));
                                pos += 4;
                        }
                        
                        resolve(galleryids);
                });
        });
};

var get_suggestions_for_query = function(query, serial) {
        return new Promise((resolve, reject) => {
                query = query.replace(/_/g, ' ');
                
                let field = 'global', term = query, results = [];
                if (query.indexOf(':') > -1) {
                        const sides = query.split(/:/);
                        field = sides[0];
                        term = sides[1];
                }

                if (/^(?:sort|order)by(?:key|direction)?:/.test(query)) {
                        if (/^(?:sort|order)by:/.test(query)) {
                                results.push({ s: 'date',          t: undefined, u: '/', n: field });
                                results.push({ s: 'datepublished', t: undefined, u: '/date/published-all.html', n: field });
                                results.push({ s: 'popular',       t: undefined, u: '/popular/year-all.html', n: field });
                                results.push({ s: 'random',        t: undefined, u: '/search.html?orderbydirection:random', n: field });
                        } else if (/^(?:sort|order)bydirection:/.test(query)) {
                                results.push({ s: 'asc',           t: undefined, u: '/search.html?orderbydirection:asc', n: field });
                                results.push({ s: 'desc',          t: undefined, u: '/', n: field });
                                results.push({ s: 'random',        t: undefined, u: '/search.html?orderbydirection:random', n: field });
                        } else { //orderbykey
                                if (/\b(?:sort|order)by:popular/i.test($('#query-input').val())) {
                                        results.push({ s: 'today',      t: undefined, u: '/popular/today-all.html', n: field });
                                        results.push({ s: 'week',       t: undefined, u: '/popular/week-all.html',  n: field });
                                        results.push({ s: 'month',      t: undefined, u: '/popular/month-all.html', n: field });
                                        results.push({ s: 'year',       t: undefined, u: '/popular/year-all.html',  n: field });
                                } else {
                                        results.push({ s: 'added',      t: undefined, u: '/', n: field });
                                        results.push({ s: 'published',  t: undefined, u: '/date/published-all.html', n: field });
                                }
                        }
                        
                        resolve([results, serial]);
                        return;
                }
                
                const chars = term.split('').map(encode_search_query_for_url);
                let url = `//${tag_index_domain}/`+field;
                if (chars.length) {
                        url += '/'+chars.join('/');
                }
                url += '.json';

                const suggestions = fetch(url).then(response => response.json()).then(suggestions => {
                        for (const suggestion of suggestions) {
                                const ns = suggestion[2];
                
                                const tagname = sanitize(suggestion[0]);
                                let url = '/'+ns+'/'+tagname+separator+'all'+separator+'1'+extension;
                                if (ns === 'female' || ns === 'male') {
                                        url = '/tag/'+ns+':'+tagname+separator+'all'+separator+'1'+extension;
                                } else if (ns === 'language') {
                                        url = '/index-'+tagname+separator+'1'+extension;
                                }

                                results.push({ s: suggestion[0], t: suggestion[1], u: url, n: ns, });
                        }
                        resolve([results, serial]);
                }).catch(error => { resolve([[], serial]); });
                
//                 const key = hash_term(term);
//         
//                 get_node_at_address(field, 0, serial).then((node) => {
//                         if (!node) {
//                                 resolve([[], serial]);
//                                 return;
//                         }
//                         
//                         B_search(field, key, node, serial).then((data) => {
//                                 if (!data) {
//                                         resolve([[], serial]);
//                                         return;
//                                 }
//                                 
//                                 get_suggestions_from_data(field, data).then((results) => resolve([results, serial]));
//                         });
//                 });
        });
};

var nozomi_address_from_state = function(state) {
        if (state.orderby !== 'date' || state.orderbykey === 'published') {
                if (state.area === 'all')
                        //ltn.hitomi.la/popular/year-all.nozomi
                        return '//'+[domain, compressed_nozomi_prefix, state.orderby, [state.orderbykey, state.language].join('-')].join('/')+nozomiextension;
                //ltn.hitomi.la/tag/popular/week/female:sole%20female-czech.nozomi
                return '//'+[domain, compressed_nozomi_prefix, state.area, state.orderby, state.orderbykey, [state.tag, state.language].join('-')].join('/')+nozomiextension;
        }
        
        if (state.area === 'all')
                return '//'+[domain, compressed_nozomi_prefix, [state.tag, state.language].join('-')].join('/')+nozomiextension;
        return '//'+[domain, compressed_nozomi_prefix, state.area, [state.tag, state.language].join('-')].join('/')+nozomiextension;
};

var get_galleryids_from_nozomi = function(state) {
        return new Promise((resolve, reject) => {
                var xhr = new XMLHttpRequest();
                xhr.open('GET', nozomi_address_from_state(state), true);
                xhr.responseType = "arraybuffer";
                xhr.onreadystatechange = function(oEvent) {
                        if (xhr.readyState === 4) {
                                var nozomi = [];
                                if (xhr.status === 200) {
                                        var arrayBuffer = xhr.response; // Note: not oReq.responseText
                                        if (arrayBuffer) {
                                                var view = new DataView(arrayBuffer);
                                                var total = view.byteLength/4;
                                                for (var i = 0; i < total; i++) {
                                                        nozomi.push(view.getInt32(i*4, false /* big-endian */));
                                                }
                                        }
                                }
                                resolve(nozomi);
                        }
                };
                xhr.send();
        });
};

var get_galleryids_for_query = function(query, { ...state }) {
        return new Promise((resolve, reject) => {
                query = query.replace(/_/g, ' ');
                
                if (query.indexOf(':') > -1) {
                        const [left_side, right_side] = query.split(/:/);
                        
                        if ('female' === left_side || 'male' === left_side) {
                                state['area'] = 'tag';
                                state['tag'] = query;
                        } else if ('language' === left_side) {
                                state['language'] = right_side;
                        } else {
                                state['area'] = left_side;
                                state['tag'] = right_side;
                        }
                        
                        get_galleryids_from_nozomi(state).then(resolve);
                        
                        return;
                }
                
                
                const key = hash_term(query);
                const field = 'galleries';
        
                get_node_at_address(field, 0).then((node) => {
                        if (!node) {
                                resolve([]);
                                return;
                        }
                        
                        B_search(field, key, node).then((data) => {
                                if (!data) {
                                        resolve([]);
                                        return;
                                }
                                
                                get_galleryids_from_data(data).then(resolve);
                        });
                });
        });
};
