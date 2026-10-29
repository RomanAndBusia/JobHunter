"""Read the selected presentation value from a native or React-style control."""
async def display_value(locator):
    tag = await locator.evaluate('e => e.tagName.toLowerCase()')
    if tag in {'input', 'textarea'}:
        value = await locator.input_value()
        if value or await locator.get_attribute('role') != 'combobox': return value
        return await locator.evaluate('''e => {
          for(let p=e.parentElement,depth=0;p&&p.tagName!=='FORM'&&depth<5;p=p.parentElement,depth++){
            if(p.querySelectorAll('[role=combobox]').length!==1)break;
            const selected=p.querySelector('.select__single-value,[class$="-singleValue"],[data-selected-value]');
            if(selected)return (selected.getAttribute('data-selected-value')||selected.innerText||'').trim();
          }return '';
        }''')
    return await locator.get_attribute('value') or await locator.get_attribute('aria-valuetext') or ' '.join((await locator.inner_text()).split())
