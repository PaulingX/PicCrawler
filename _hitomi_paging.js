function paging_string(basename, separator, page, extension) {
        var retval = '';
        
        if (extension) {
                if (basename === '/index-all') {
                        retval = '/';
                } else {
                        retval = basename + extension;
                }
                if (page !== 1) {
                        retval += '?page=' + page;
                }
        } else { //search
                retval = basename + page;
        }
        
        return retval;
}

function insert_paging(basename, separator, extension, page_number, last_page_number) {
    $(".page-container ul").html('');
    var page = 1;
    var html = "<li><a href='" + paging_string(basename, separator, page, extension) + "'>" + page + "</a></li>";
    if (page_number > 1) {
        $(".page-container ul").append(html);
    }
    if (page_number > 4) {
        $(".page-container ul").append("<li>...</li>");
    }
    if (page_number > 3) {
        page = page_number - 2;
        html = "<li><a href='" + paging_string(basename, separator, page, extension) + "'>" + page + "</a></li>";
        $(".page-container ul").append(html);
    }
    if (page_number > 2) {
        page = page_number - 1;
        html = "<li><a href='" + paging_string(basename, separator, page, extension) +"'>" + page + "</a></li>";
        $(".page-container ul").append(html);
    }
    
    page = page_number;
    html = "<li>" + page +"</li>";
    $(".page-container ul").append(html);
    
    if ((page_number + 1) <= last_page_number) {
        page = page_number + 1;
        html = "<li><a href='" + paging_string(basename, separator, page, extension) + "'>" + page + "</a></li>";
        $(".page-container ul").append(html);
    }
    if ((page_number + 2) <= last_page_number) {
        page = page_number + 2;
        html = "<li><a href='" + paging_string(basename, separator, page, extension) + "'>" + page + "</a></li>";
        $(".page-container ul").append(html);
    }
    if ((page_number + 3) <= last_page_number) {
        if ((page_number + 4) <= last_page_number) {
            $(".page-container ul").append("<li>...</li>");
        }
        page = last_page_number;
        html = "<li><a href='" + paging_string(basename, separator, page, extension) + "'>" + page + "</a></li>";
        $(".page-container ul").append(html);
    }
}
