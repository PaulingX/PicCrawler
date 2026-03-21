"use strict";

var results_array = {}, results = [];
var results_per_page = 25;
var outstanding_requests = {};
var number_of_outstanding_requests = 0;
let state;


function do_search() {
        start_loading_timer();
        
        let query_string = decodeURIComponent(window.location.search).replace(/^\?/, '');
        
        $('#query-input').val(query_string);
        
        let terms = query_string.toLowerCase().trim().split(/\s+/);
        let positive_terms = [], negative_terms = [], or_terms = [[]];
        
        state = {
                area: 'all',
                tag: 'index',
                language: 'all',
                orderby: 'date',
                orderbykey: undefined,
                orderbydirection: 'desc',
        };
        
        $.each(terms, function(i, term) {
        	term = term.replace(/_/g, ' ');
        	
                if (term.match(/^(?:sort|order)by(?:key|direction)?:/)) {
                        const [left_side, right_side] = term.split(/:/);
                        
                        //orderbykey:
                        if (left_side.match(/^(?:sort|order)(?:by)?key$/)) {
                                state.orderbykey = right_side.replace(/[^0-9a-z]/g, '');
                        
                        //orderby:
                        } else if (right_side === 'popular' || right_side === 'popularity') {
                                state.orderby = 'popular';
                        } else if (right_side === 'date') {
                                state.orderby = 'date';
                        } else if (right_side === 'datepublished') {
                                state.orderby = 'date';
                                state.orderbykey = 'published';
                        } else if (/^(?:sort|order)by$/.test(left_side) && (right_side === 'random' || right_side === 'rand')) {
                                state.orderbydirection = 'random';
                        
                        //orderbydirection:
                        } else if (left_side === 'orderbydirection' || left_side === 'sortbydirection') {
                                state.orderbydirection = right_side.replace(/[^0-9a-z]/g, '');
                        }
                        
        	        return;
        	}
        	
        	if (term === 'or') return;
        	const or_previous = (i > 0 && terms[i-1] === 'or');
        	const or_next = (i+1 < terms.length && terms[i+1] === 'or');
        	if (or_previous || or_next) {
        	        or_terms[or_terms.length-1].push(term);
                        if (!or_next) {
                                or_terms.push([]);
                        }
        	        return;
        	}
                
                if (term.match(/^-/)) {
                        negative_terms.push(term.replace(/^-/, ''));
                } else {
                        positive_terms.push(term);
                }
        });
        positive_terms.sort((a, b) => {
                if (/:/.test(a)) {
                        if (!/:/.test(b)) {
                                return -1;
                        }
                        return 0;
                }
                if (/:/.test(b)) {
                        return 1;
                }
                return 0;
        });
        or_terms = or_terms.filter(a => a.length);
        
        if (state.orderbykey === undefined) {
                if (state.orderby === 'popular') {
                        state.orderbykey = 'year';
                } else { //date
                        state.orderbykey = 'added';
                }
        }
        
        let order_results = (results) => { //https://stackoverflow.com/a/12646864
                let shuffleArray = (array) => {
                        for (let i = array.length - 1; i > 0; i--) {
                                const j = Math.floor(Math.random() * (i + 1));
                                [array[i], array[j]] = [array[j], array[i]];
                        }
                };

                if (state.orderbydirection === 'asc' || state.orderbydirection === 'ascending') {
                        results.reverse();
                } else if (state.orderbydirection === 'rand' || state.orderbydirection === 'random') {
                        shuffleArray(results);
                }
        };
        

        new Promise((resolve, reject) => { //initial results
                if (!positive_terms.length || (!/:/.test(positive_terms[0]) && state.orderbykey !== 'added')) {
                        get_galleryids_from_nozomi(state).then(new_results => {
                                results = new_results;
                                order_results(results);
                                resolve();
                        });
                } else {
                        const term = positive_terms.shift();
                        get_galleryids_for_query(term, state).then(new_results => {
                                results = new_results;
                                order_results(results);
                                resolve();
                        });
                }
        }).then(() => { //OR results
                return Promise.all(or_terms.map(termlist => {
                        let o = new Set();
                        return Promise.all(termlist.map(term => {
                                return new Promise((resolve, reject) => {
                                        get_galleryids_for_query(term, state).then(new_results => {
                                                new_results.map(r => o.add(r));
                                                resolve();
                                        });
                                });
                        })).then(() => {
                                results = results.filter(galleryid => o.has(galleryid));
                        });
                }));
        }).then(() => { //positive results
                return Promise.all(positive_terms.map(term => {
                        return new Promise((resolve, reject) => {
                                get_galleryids_for_query(term, state).then(new_results => {
                                        const new_results_set = new Set(new_results);
                                        results = results.filter(galleryid => new_results_set.has(galleryid));
                                        resolve();
                                });
                        });
                }));
        }).then(() => { //negative results
                return Promise.all(negative_terms.map(term => {
                        return new Promise((resolve, reject) => {
                                get_galleryids_for_query(term, state).then(new_results => {
                                        const new_results_set = new Set(new_results);
                                        results = results.filter(galleryid => !new_results_set.has(galleryid));
                                        resolve();
                                });
                        });
                }));
        }).then(() => {
                const final_results_length = results.length;
                $('#number-of-results').html(final_results_length+' Result'+(final_results_length === 1 ? '' : 's'));
                if (!final_results_length) {
                        hide_loading();
                        $('.gallery-content').removeAttr('style');
                        $('.gallery-content').html($('#no-results-content').html());
                } else {
                        put_results_on_page();
                }
        });
}


function get_block(url) {
        $.get(url, function(data, status) {
                if (status === 'success') {
                        delete outstanding_requests[url];
                        --number_of_outstanding_requests;
                        results_array[url] = rewrite_tn_paths(data);
                        put_results_on_page();
                }
        });
}

function put_results_on_page() {
        var page_number = 1;
        var hash = document.location.hash.replace(/^#/, '');
        if (hash.length) {
                page_number = parseInt(hash);
        }
        
        var final_results_length = results.length;
        var number_of_pages = Math.ceil(final_results_length/results_per_page);
        var last_page_number = number_of_pages;
        
        var datas = [];
        for (var i = (page_number-1)*results_per_page; i < page_number*results_per_page && i < final_results_length; ++i) {
        	var result = results[i];
                var url = '//'+domain+'/'+galleryblockdir+'/'+result+extension;
                if (results_array[url]) {
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
                if (state.orderby === 'date' && state.orderbykey === 'published') {
                        set_date_published();
                }
                localDates();
                limitLists();
        
                insert_paging('#', '', '', page_number, last_page_number);
                if ('loading' in HTMLImageElement.prototype) {
                        flip_lazy_images();
                }
        });
}

function set_orderbydropdown_value() {
        const orderbydropdown = document.getElementById("orderbydropdown");
        if (!orderbydropdown) return;
        
        const query_string = decodeURIComponent(window.location.search).replace(/^\?/, '');

        orderbydropdown.value = 'date_added';
        if (/(?:sort|order)by(?:direction)?:rand/.test(query_string)) {
                orderbydropdown.value = 'random';
        } else if (/(?:(?:sort|order)by:datepublished|(?:sort|order)bykey:published)/.test(query_string)) {
                orderbydropdown.value = 'published';
        } else if (/(?:sort|order)by:popular/.test(query_string)) {
                orderbydropdown.value = 'year';
                let match = query_string.match(/(?:sort|order)bykey:([0-9a-z]+)/);
                if (match) {
                        orderbydropdown.value = match[1];
                }
        }
        
        orderbydropdown.addEventListener("change", function() {
                if (!orderbydropdown.value) return;
                
                let new_terms = query_string.toLowerCase().trim().split(/\s+/).filter(term => !/^(?:sort|order)by(?:key|direction)?:/.test(term));
                
                if (['today', 'week', 'month', 'year'].includes(orderbydropdown.value)) {
                        new_terms.push('orderby:popular');
                        new_terms.push('orderbykey:'+orderbydropdown.value);
                } else if (orderbydropdown.value === 'published') {
                        new_terms.push('orderby:datepublished');
                } else if (orderbydropdown.value === 'random') {
                        new_terms.push('orderbydirection:random');
                }
                
                orderbydropdown.selectedIndex = 0;
                if (new_terms.length) {
                        document.location = '/search.html?'+encodeURIComponent(new_terms.join(' '));
                } else {
                        document.location = '/';
                }
        });
}

$(window).bind('hashchange', function() {
        $('.gallery-content').html('');
        start_loading_timer();
        put_results_on_page();
        scroll_to_top();
});

$(function() {
        set_orderbydropdown_value();
        
        get_index_version('galleriesindex').then((string) => {
                galleries_index_version = string;
                do_search();
        });
});
